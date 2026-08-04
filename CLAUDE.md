# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Backend (Python 3.12, `uv`; tool versions via `mise.toml`):

```sh
uv sync
uv run pytest -q                                  # full suite
uv run pytest tests/test_scoring.py -v            # one file
uv run pytest tests/test_scoring.py::test_name    # one test
uv run ruff check .                               # line-length 100
```

`tests/assets/` generates fixture media with real `ffmpeg` — those tests fail
without `ffmpeg` on PATH (CI installs it).

Run the backend locally (Vite dev proxy expects port 8000):

```sh
IMPOSTARR_CONFIG=/path/to/impostarr.yml uv run uvicorn --factory impostarr.main:app --port 8000
```

Frontend (`web/`, React 19 + Vite + Tailwind 4 + vitest):

```sh
cd web && npm ci
npm test                                # vitest run
npm test -- src/components/QueueTable.test.tsx
npm run lint                            # oxlint
npm run build                           # tsc -b && vite build -> web/dist
```

End-to-end and image checks: `bash demo/e2e.sh` (full throwaway Sonarr +
Impostarr + synthetic library stack, `--down` for CI mode) and
`bash docker/smoke.sh`. Both are slow and spin up Docker.

CI (`.github/workflows/build.yml`) runs ruff, pytest, vitest, `npm run build`,
then builds/pushes the image. Keep all four green.

## Architecture

Single FastAPI process. `main.py` is the composition root: it loads
`Settings`, runs Alembic to head, installs+activates the external-plugin venv
overlay, loads identifier plugins and the transcriber backend, builds one
`PipelineDeps` per Sonarr instance, starts the `WorkerPool` in the lifespan,
and mounts `web/dist` as an SPA under `/`. There is deliberately no
module-level `app = create_app()`.

Data flow for one file:

1. **`discovery.py`** — polls Sonarr history (`poll_once`) or walks the
   library (`backfill_step`) per instance, creating `files` + `jobs` rows.
   Whole batch plus its watermark/cursor commits exactly once so the cursor
   can never outrun the rows; that is why it builds `Job` objects directly
   instead of calling `jobs.create_job` (which commits per call).
2. **`jobs.py`** — the queue state machine over the `jobs` table.
   `VALID_TRANSITIONS` is the single source of truth for every status change.
   Claiming is SELECT-then-conditional-UPDATE (no in-process locking, so
   remote workers stay possible); `heartbeat`/`release` are fenced on
   `claimed_by`, `park`/`unpark`/`requeue`/`set_status_checked` on the
   expected source status. A lost fence raises `LeaseLost`/`InvalidTransition`
   rather than clobbering the current holder. Every function commits its own
   session — never share a session across one of these calls and other
   uncommitted work.
3. **`worker.py`** — orchestration glue only. Each worker-loop task gets its
   own `worker_id` (`dataclasses.replace` of the shared `PipelineDeps`) so the
   `claimed_by` fence can tell concurrent tasks apart while the series-context
   cache stays shared. Also hosts the lease reaper, hourly trash sweep, and
   per-instance discovery loops. Throttling (`throttle.py`: active hours,
   pool-wide `jobs_per_hour` token bucket, runtime pause flag) is checked here.
4. **`pipeline.py`** — `process_job` runs asset extraction (probe → audio →
   subs → frames → transcript, each cached by `assets.extract.fingerprint()`
   against the `assets` table), dupe detection over stored frame hashes, the
   plugin stage, scoring/routing, verdict persistence, remediation, and phash
   corpus writes. DB access is synchronous inside the coroutine by design.
5. **`normalize.py`** — pure mapping of plugin candidates to Sonarr episode
   ids (tvdb/tmdb/absolute/scene numbering, multi-episode all-or-nothing).
6. **`scoring.py`** — pure `aggregate()` (pool per-candidate confidences into
   `s_claimed`/`s_alt`) then `route()` (thresholds + per-instance auto flags →
   `matched`/`quarantine`/`inconclusive`/`remediate`). No I/O, no series
   context; it only sees normalized keys.
7. **`remediate.py`** — `remap` (hardlink into staging + Sonarr manual import)
   or `replace` (blocklist + delete + re-search, trashing via `trash.py`).
   Each step appends to `verdict.remediation_log` and commits before the next,
   so a crash leaves an accurate partial log.

`api/routes.py` is the whole REST surface (`/api/v1/...`) plus an SSE
`/events` stream fed by `api/events.EventBus`; `api/logbuffer.py` backs
`/logs`. The UI drives everything through it — queues, job detail, verdict
submission, approve/reject/replace/park/rerun, trash, backfill.

### Load-bearing invariants

- **Plugins never fabricate evidence.** A `confidence` of `0.0` means "I
  compared and found no similarity", never "I could not measure this" — the
  latter must be an `abstain`. See `docs/scoring.md`; a regression here
  (`whisper-subs` emitting a fake 0.0) is what motivated log-odds fusion and
  outlier rejection.
- **Plugin cache reuses only `status="ok"` rows.** `abstain`/`error` are
  environmental/transient; replaying them would freeze a job forever since the
  input fingerprint never changes. Rows are append-only (one per execution).
- **`ok` results must include a candidate with `ident.series == "claimed"`**
  (enforced by `PluginResult`'s validator). If none of a plugin's candidates
  normalize to the claimed episode-id set, `aggregate()` treats it as a 0.0
  report on the claimed key.
- **`dry_run` suppresses only Sonarr API mutations and media-library
  filesystem writes.** Impostarr's own DB rows, extracted assets, and phash
  corpus still get written, and jobs still walk to `remediated`; suppressed
  actions log `DRY-RUN: would ...`.
- **Remediation is non-resumable.** Re-entry is blocked only when the log
  contains a *mutating*, successful, non-DRY-RUN step (`MUTATING_STEPS`), so
  refusals and notes never wedge a legitimate retry.

### Plugins

`whisper-subs`, `subs-llm`, `transcript-llm` live in their own packages under
`src/impostarr_plugin_*/` and are ordinary third-party-shaped plugins — they
import only from `impostarr.plugins.*` (plus `impostarr.llm`) and register via
the `impostarr.identifiers` entry-point group in the root `pyproject.toml`.
Use them as reference implementations; keep them free of private-core imports.
Transcriber backends use the parallel `impostarr.transcribers` group with a
factory-function convention (`plugins/transcribers.py`). The contract lives in
`plugins/base.py` — `identify()` must never raise.

### Config

`config.py` — pydantic-settings, one YAML file (`IMPOSTARR_CONFIG`, default
`/config/impostarr.yml`), with `IMPOSTARR__SECTION__KEY` env overrides that
always win. Every new option needs: the pydantic field with a docstring/comment
explaining behaviour, an annotated entry in `examples/impostarr.yml`, and a
README row if user-facing. Missing config file → defaults, not an error.

### Database

SQLAlchemy 2 declarative models in `models.py`; SQLite by default (WAL, 30s
busy timeout) or Postgres via `db.dsn`. Schema changes need an Alembic
revision under `src/impostarr/migrations/versions/` — `init_db` upgrades to
head on every startup, so a model change without a migration silently
diverges. `alembic.ini` exists for CLI autogenerate only; the app builds its
`Config` programmatically.

### Frontend

`web/src/api/` is the only place that talks HTTP (`client.ts`) or SSE
(`sse.ts`); `types.ts` mirrors the API's response shapes by hand — update it
alongside `api/routes.py` changes. `App.tsx` owns queue/tab/pagination state
and uses per-fetch-kind request tokens plus refs so slow responses can't
clobber newer ones. The inspect panel is composed from
`InspectModal` + `Lhs/RhsPanel` + section components, with data shaping in
`lib/inspectData.tsx`. Components are colocated with `*.test.tsx`.

## Working on the UI

Component tests pass while the rendered page is broken — this has happened
repeatedly (collapsed grid columns, dead-looking sort headers, raw database
ids in cells, a wrapper div silently defeating a width). jsdom does not run
CSS layout, so a passing vitest suite is not evidence that anything looks or
works right.

Before claiming any UI change is done:

- Run it against real data (see `docs/local-deployment.md` for the
  port-forward + dev-proxy recipe) and screenshot it with Playwright.
- **Assert layout with numbers**, not eyeballs: `getBoundingClientRect` /
  `getComputedStyle` for widths, ratios, alignment, gaps, and
  `cursor: pointer` on anything clickable. Report the measurements.
- Exercise the interaction, not just the render: click the header and assert
  both the request *and* that the rows reordered; click the checkbox and
  assert the DOM state changed.
- Check the cells contain human-meaningful values. `Series 4` / `699` passed
  every structural test for four rounds while being useless to a human.

**Copy is data, not prose to re-author.** Button labels and explanation
strings are specified; reuse the exact strings rather than rewriting them in
a new voice each time, and never reference layout position in copy ("the
button below") — it goes stale the moment anything moves.

## Conventions

- Module docstrings carry the design rationale ("why this shape, what it
  guards against"). When changing behaviour in `jobs.py`, `pipeline.py`,
  `worker.py`, `remediate.py`, or `scoring.py`, update the docstring in the
  same edit — they are the primary spec for these invariants.
- Branch per change off `main` (`feat/...`, `fix/...`), merge back with a
  `Merge <branch>: <summary>` commit. Worktrees go in `.worktrees/`
  (gitignored).
- `docs/` holds the original spec (`docs/specs/`), the PoC plan
  (`docs/plans/`), `docs/scoring.md` (fusion/outlier rationale),
  `docs/BACKLOG.md` (agreed-but-unstarted work, with the decisions already
  settled), `docs/integrations.md` (hard-won Sonarr/OpenSubtitles/provider
  behaviour — read before touching those clients), `docs/llm-identification.md`
  (measured results on what LLMs can identify, and why the prompts are shaped
  the way they are), and `docs/ROADMAP.md` (non-committal future direction).
  `docs/local-deployment.md` is gitignored operator notes.
- **External-service behaviour is verified live, not from docs or mocks.**
  Several integrations behave differently from their documentation
  (`docs/integrations.md`); a mocked test proves only that the code matches
  the mock.
