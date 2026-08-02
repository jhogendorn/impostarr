# Impostarr PoC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Tasks use checkbox syntax for tracking. This plan is a SPEC: it defines
> interfaces, behaviors, and verification — the implementer writes the code.
> TDD per task: write the failing test(s) described, see them fail, implement
> minimally, see them pass, commit with the given message prefix.

**Goal:** Working Impostarr PoC per `docs/specs/2026-08-02-impostarr-poc-design.md`
(the converged spec — authoritative on all behavior; read it before any task).

**Architecture:** Single FastAPI service + internal async worker pool over a
DB-backed job queue; pluggable identifier plugins behind entry points;
Sonarr-API-driven discovery and remediation; React/Tailwind queue UI; docker
deployment. SQLite default, Postgres optional.

**Tech stack (locked):** Python 3.12, uv, FastAPI, uvicorn, SQLAlchemy 2.x +
Alembic, pydantic v2 + pydantic-settings, httpx (+respx for tests), pytest +
pytest-asyncio, ruff, faster-whisper, imagehash+Pillow, mkv-episode-matcher
(PyPI, pinned) , tvidentify (git, pinned), Vite + React + TypeScript +
Tailwind + Headless UI. mise for tool versions.

**Branch:** work on `poc` branched from `main`.

**Conventions for every task:** tests live under `tests/` mirroring
`src/impostarr/`; run `uv run pytest <file> -v` and `uv run ruff check .`
before each commit; commit messages `feat|test|chore(scope): ...`. Async code
uses asyncio throughout. All timestamps UTC. Never call real network in tests
(respx-mock Sonarr/OpenSubtitles/LLM).

---

### Task 1: Project scaffold

**Files:** Create `pyproject.toml`, `mise.toml`, `.gitignore`,
`src/impostarr/__init__.py`, `tests/__init__.py`, `README.md` (one-paragraph
purpose + pointer to spec).

- [ ] `git checkout -b poc`
- [ ] `uv init` layout with `src/` packaging; deps: fastapi, uvicorn[standard],
  sqlalchemy, alembic, pydantic, pydantic-settings, httpx, pyyaml; dev deps:
  pytest, pytest-asyncio, respx, ruff. Python pinned 3.12 in `mise.toml`.
- [ ] Smoke test `tests/test_scaffold.py`: `import impostarr` and assert
  `impostarr.__version__ == "0.1.0"`.
- [ ] Verify: `uv run pytest -v` passes, `uv run ruff check .` clean.
- [ ] Commit `chore: scaffold impostarr project`.

### Task 2: Configuration

**Files:** Create `src/impostarr/config.py`, `tests/test_config.py`,
`examples/impostarr.yml` (fully commented example matching the spec's
Configuration section).

Pydantic-settings models mirroring the spec Configuration section exactly:
`Settings` root with sections `sonarr: list[SonarrInstance]`, `thresholds:
Thresholds` (keys `quarantine=0.8, auto=0.4, alt=0.8, alt_margin=0.2,
auto_min_evidence=2, phash_store=0.9` as defaults), `plugins`, `refsubs`,
`auth`, `db`, `workers`. `SonarrInstance` fields per spec: `name` (unique),
`url`, `api_key`, `path_mappings: list[{sonarr: str, local: str}]`,
`staging_dir`, `watch_dirs: list[str]` (empty = all), `poll_interval_s`
(default 300), `auto_remap: bool = false`, `auto_replace: bool = false`.

Loading: `load_settings(path: Path | None) -> Settings` reads YAML at
`/config/impostarr.yml` (overridable arg/env `IMPOSTARR_CONFIG`), then env
overrides via pydantic-settings `env_prefix="IMPOSTARR__"`,
`env_nested_delimiter="__"`; list/object-valued env values are JSON-decoded
(pydantic-settings does this natively for complex types).

- [ ] Failing tests: YAML load round-trip; scalar env override
  (`IMPOSTARR__THRESHOLDS__AUTO=0.3`); JSON list env override
  (`IMPOSTARR__SONARR='[{...minimal instance...}]'`); defaults applied;
  duplicate instance names rejected; missing file → defaults-only Settings.
- [ ] Implement; verify; commit `feat(config): settings with YAML + env override`.

### Task 3: Database layer and models

**Files:** Create `src/impostarr/db.py`, `src/impostarr/models.py`,
`alembic.ini`, `src/impostarr/migrations/`, `tests/test_models.py`.

`db.py`: engine factory from `Settings.db` (absent DSN → SQLite at
`<state_dir>/impostarr.db` with WAL pragma; DSN → Postgres), session factory,
`init_db()` applying Alembic migrations programmatically at startup.

`models.py`: SQLAlchemy 2.x declarative tables exactly as the spec Data model
section: `instances`, `jobs`, `files`, `assets`, `plugin_results`,
`verdicts`, `frame_hashes`, `phash_corpus`. Portable types only (JSON, not
JSONB/ARRAY). Key constraints: `jobs.status` in
`(hold,pending,active,matched,quarantine,inconclusive,error,remediated)`;
unique `(instance_id, episode_file_id)` on `files`; `jobs` lease fields
`claimed_by, claimed_at, heartbeat_at`; `plugin_results.input_fingerprint`
indexed; `verdicts` stores `s_claimed, s_alt, outcome, proposed_action JSON,
remediation_log JSON, source`.

- [ ] Failing tests: `init_db()` creates schema on fresh SQLite; insert/query
  one row per table; uniqueness constraint enforced; JSON columns round-trip.
- [ ] Single initial Alembic migration; verify; commit
  `feat(db): schema + migrations`.

### Task 4: Sonarr API client

**Files:** Create `src/impostarr/sonarr/__init__.py`,
`src/impostarr/sonarr/client.py`, `src/impostarr/sonarr/types.py`,
`tests/sonarr/test_client.py`.

`SonarrClient` (httpx.AsyncClient, `X-Api-Key` header, base `/api/v3`) with
typed methods (pydantic models in `types.py`, modeled from the Sonarr v3 API;
only fields we consume):

- `system_status()` — connectivity check.
- `history_since(history_id: int) -> list[HistoryRecord]` — paged
  `GET /history?eventType=downloadFolderImported&sortKey=id&sortDirection=descending`,
  early-stopping as soon as a page contains an id ≤ watermark (avoids
  re-walking lifetime history each poll); returns matching records sorted
  ascending by id. HistoryRecord: id, episodeIds,
  seriesId, sourceTitle, downloadId, data (guid, indexer), episodeFileId,
  quality, languages, date.
- `episode_files(series_id)` / `episode_file(id)` — path, size, quality.
- `series(series_id)` / `all_series()` — title, tvdbId, tmdbId, imdbId, ids map.
- `episodes(series_id) -> list[Episode]` — id, seasonNumber, episodeNumber,
  absoluteEpisodeNumber, sceneSeasonNumber, sceneEpisodeNumber,
  sceneAbsoluteEpisodeNumber, episodeFileId, hasFile.
- `delete_episode_file(id)`.
- `mark_history_failed(history_id)` — `POST /history/failed/{id}` (blocklists).
- `command(name, **body)` — `POST /command`; used for `EpisodeSearch`
  (episodeIds) and `ManualImportCommand`... **verified choreography note:**
  manual import is `GET /manualimport?folder=...` to enumerate importable
  items then `POST /command {name: ManualImport, files: [...], importMode:
  "move"}`. Client exposes `manual_import_candidates(folder)` and
  `execute_manual_import(files, import_mode)`.

Error model: raise `SonarrError` on non-2xx with status + body; retry (3x,
exponential) on 5xx/timeouts only.

- [ ] Failing tests (respx): each method against canned JSON fixtures in
  `tests/sonarr/fixtures/`; pagination of history; retry on 503 then success;
  `SonarrError` on 400 with no retry.
- [ ] Implement; verify; commit `feat(sonarr): typed API client`.

### Task 5: Job queue state machine

**Files:** Create `src/impostarr/jobs.py`, `tests/test_jobs.py`.

Interface: `create_job(session, file_id) -> Job` (state `pending`; `hold` on
rate-limit/park), `claim_next(session, worker_id) -> Job | None` (atomic
UPDATE...WHERE status='pending' ordered by created_at; sets active + lease),
`heartbeat(job)`, `release(job, new_status, **result_fields)`,
`reap_stale(session, lease_timeout_s)` (active + stale heartbeat → pending,
increment `attempts`; attempts > 3 → error), `park(job)`/`unpark(job)`.
Transitions validated against the spec queue model; invalid transition raises.

- [ ] Failing tests: claim is exclusive under two concurrent claimers (two
  sessions, one wins); lease reap requeues; attempts cap → error; park/unpark;
  invalid transition raises.
- [ ] Implement; verify; commit `feat(jobs): DB-backed claim/lease queue`.

### Task 6: Discovery (history polling + backfill)

**Files:** Create `src/impostarr/discovery.py`, `tests/test_discovery.py`.

`Discoverer(instance_cfg, client, session_factory)` with:
- `poll_once()` — fetch history since stored watermark; for each import
  record: map sonarr path → local path via `path_mappings` (longest-prefix
  match; unmapped → skip + warn), apply `watch_dirs` filter, dedupe by
  `(instance, episode_file_id)`, capture file row with all remediation
  metadata (spec Data model `files`), create job, advance watermark (only
  after batch committed — crash-safe).
- `backfill_step(batch_size)` — walk `all_series()`/`episode_files()` with
  persisted `(series_id, episode_file_id)` cursor on `instances`; same file
  capture path. PoC decision (settled): no rate limiter in discovery —
  backfill jobs go directly to `pending` and the caller paces via
  batch_size/call cadence; jobs.py's `hold`/park support is the future
  limiter seam. Documented in the Discoverer docstring.
- Content hash: xxh64 of first+last 8MiB + size (fast, stable) via
  `hash_file(path)` helper — full-file hashing is too slow on remuxes.

- [ ] Failing tests: watermark advances only on success; path mapping picks
  longest prefix; watch_dirs filter; dedupe on second poll; backfill cursor
  resumes mid-series; unmapped path skipped with warning log.
- [ ] Implement; verify; commit `feat(discovery): history polling + backfill`.

### Task 7: Asset extraction

**Files:** Create `src/impostarr/assets/__init__.py`,
`src/impostarr/assets/extract.py`, `src/impostarr/assets/transcribe.py`,
`tests/assets/test_extract.py`, `tests/assets/conftest.py` (fixture builder).

`extract.py` — pure-ffmpeg/ffprobe helpers, each returning an `Asset` record
(type, path under assets dir, fingerprint, tool metadata):
- `probe(path)` — ffprobe JSON (streams, duration, container).
- `extract_audio(path, out, offset_s, duration_s)` — 16kHz mono wav slice
  (defaults: skip first 60s, take 15 min or full duration if shorter).
- `extract_embedded_subs(path, out_dir)` — text subs via ffmpeg; PGS/VobSub
  extracted raw for the OCR plugin; returns list per stream with language.
- `sample_frames(path, out_dir, n=16)` — frames at deterministic timestamps
  (i+0.5)/n of duration; per-frame perceptual hash (imagehash.phash, 64-bit)
  → `FrameHashSeq {algo: "phash", version: 1, timestamps, hashes}` stored in
  `frame_hashes`; also writes jpeg thumbnails (max 480px wide) as assets.
- `hamming_similarity(seq_a, seq_b) -> float` — mean per-frame similarity,
  alignment by index; used later by dupe detection.

`transcribe.py` — `Transcriber` protocol (`transcribe(wav_path) ->
TranscriptResult {segments: [{start, end, text}], language}`) with
`FasterWhisperTranscriber` (lazy model load from `/models`, device from
config) and `NullTranscriber` for tests. faster-whisper is an optional import
guarded at runtime.

Test fixture: `conftest.py` generates a 30s test video once per session via
ffmpeg `testsrc2` + `sine` + a burned/embedded SRT stream (exact command in
conftest; skip suite cleanly with a message if ffmpeg absent).

- [ ] Failing tests: probe returns duration≈30; audio wav exists w/ expected
  sample rate; embedded SRT extracted with expected text; 16 frame hashes,
  deterministic across two runs on same file; hamming_similarity(x,x)==1.0;
  asset rows persisted with fingerprints.
- [ ] Implement; verify; commit `feat(assets): extraction + phash + transcriber`.

### Task 8: Reference subtitle service

**Files:** Create `src/impostarr/refsubs.py`, `tests/test_refsubs.py`.

`RefSubService(cfg, http)` — `get(series_ext_ids, season, episode) -> Path |
None`: check manual drop-dir (`<manual_dir>/<tvdb_id>/SxxEyy.srt`) first,
then cache dir, then OpenSubtitles REST API (`Api-Key` header, JWT login,
search by tvdb id + season/episode, download best-rated en subtitle).
Persistent daily download counter (row in `instances`-adjacent `kv` table or
JSON file in state dir — implementer's choice, must survive restart); at
quota → return None (callers abstain). All failures → log + None, never
raise into plugins.

- [ ] Failing tests (respx): manual dir precedence; cache hit skips network;
  API success path caches file; quota exhaustion returns None; API 5xx
  returns None.
- [ ] Implement; verify; commit `feat(refsubs): reference subtitle service`.

### Task 9: Plugin contract, loader, and normalization

**Files:** Create `src/impostarr/plugins/__init__.py`,
`src/impostarr/plugins/base.py`, `src/impostarr/plugins/loader.py`,
`src/impostarr/normalize.py`, `tests/plugins/test_base.py`,
`tests/test_normalize.py`.

`base.py` — pydantic models exactly matching the spec Plugin contract:
`PluginResult {status: ok|abstain|error, reason, candidates:
list[Candidate]}`, `Candidate {confidence, ident: CandidateIdent | None,
numbering, evidence}`, `CandidateIdent {series: Literal["claimed"] |
ExternalIds, season, episodes: list[int]}`. Validators: numbering null iff
ident null; `ok` requires ≥1 candidate for the claimed ident. Abstract base
`IdentifierPlugin` with `name`, `version`, `async identify(claimed, assets,
ctx) -> PluginResult` and `config_model` class attr.

`loader.py` — discover via entry-point group `impostarr.identifiers`; apply
per-plugin config/enable/weight from Settings; boot-time installer
`ensure_external_plugins(specs)` runs `uv pip install` into the running env
only when the lock-hash of pinned specs (stored in state dir) changes;
install failure disables that plugin and logs, never crashes the app.

`normalize.py` — `normalize(candidate, series_ctx) -> NormalizedCandidate
{episode_ids: list[int]} | Unnormalizable(reason)`. Mapping rules per spec:
tvdb season/episode direct; absolute via `absoluteEpisodeNumber`; scene via
`sceneSeasonNumber/sceneEpisodeNumber/sceneAbsoluteEpisodeNumber` with
fallback to plain numbers when scene fields absent; tmdb treated as tvdb
standard order (documented limitation); `series != "claimed"` → normalized as
cross-series (carries ext ids, no episode_ids); ident null → junk marker.

- [ ] Failing tests: contract validators (numbering/ident coupling, claimed
  candidate required); loader discovers a test plugin registered via entry
  point in `tests/plugins/fake_plugin.py`; disabled plugin excluded; each
  normalization rule incl. multi-episode arrays and specials (season 0);
  unnormalizable recorded.
- [ ] Implement; verify; commit `feat(plugins): contract, loader, normalization`.

### Task 10: Scoring and routing

**Files:** Create `src/impostarr/scoring.py`, `tests/test_scoring.py`.

Pure functions, no I/O — this is the heart, test exhaustively:
- `aggregate(results: list[(plugin_name, weight, PluginResult)], claimed_ids)
  -> ScoreSheet {s_claimed, s_alt, alt: NormalizedCandidate | None,
  applicable_count, per_candidate: dict}` implementing the spec formula
  (weighted mean over plugins reporting each candidate; abstain/error
  excluded; candidates keyed by normalized episode-id set / cross-series /
  junk).
- `route(sheet, thresholds, instance_flags) -> RoutingDecision {outcome:
  matched|quarantine|inconclusive|remediate, action: None | Remap | Replace,
  auto: bool, reason}` implementing the spec routing table verbatim,
  including: empty applicable set → inconclusive; credible-alternate rule
  (`alt ≥ thresholds.alt` and margin); `auto_min_evidence` gate demoting
  auto→proposed; per-instance auto flags; cross-series/junk credible
  candidate → Replace; same-series credible → Remap.

- [ ] Failing tests: table-driven cases covering every routing row and edge:
  all-abstain, single-plugin below min-evidence, claimed-high, mid-band,
  low+credible-same-series-alt, low+cross-series alt, low+junk, low+no-alt,
  margin failure, weight asymmetry, tie (alt == claimed + margin exactly).
- [ ] Implement; verify; commit `feat(scoring): aggregation + routing`.

### Task 11: whisper-subs plugin (mkv-episode-matcher adapter)

**Files:** Create `src/impostarr/plugins/whisper_subs.py`,
`tests/plugins/test_whisper_subs.py`; modify `pyproject.toml` (add pinned
`mkv-episode-matcher`, entry point).

Adapter strategy (spec approach 2): import the library's subtitle-matching
internals; do NOT invoke its CLI/renaming. Behavior: for claimed episode ±
a candidate window (claimed episode's season, all episodes with reference
subs available, capped at 10 nearest by episode number), fetch reference subs
via `RefSubService`, compare against the job's whisper transcript (from
assets; if transcript asset missing and transcriber available, transcribe
now), produce per-episode match ratio → candidates with confidence = ratio
normalized to 0–1. No transcript and no transcriber → abstain("no
transcript"). No reference subs at all → abstain("no reference subtitles").
Pin exact version; wrap all library calls so an internal API change surfaces
as `error`, not a crash. If at implementation time the library's internals
prove unusable as importable functions, implement the comparison natively
(rapidfuzz token_set_ratio over normalized lines) inside this same module —
the plugin interface must not change either way.

- [ ] Failing tests: mocked RefSubService + canned transcript → correct
  ranked candidates incl. mislabel case (highest ratio on non-claimed
  episode); abstain paths; library-exception → error result.
- [ ] Implement; verify; commit `feat(plugin): whisper-subs identifier`.

### Task 12: subs-llm plugin (tvidentify adapter)

**Files:** Create `src/impostarr/plugins/subs_llm.py`,
`tests/plugins/test_subs_llm.py`; modify `pyproject.toml` (pinned git dep,
entry point).

Adapter over tvidentify's approach: take embedded-sub text assets (OCR via
tesseract for PGS/VobSub — invoked through the library where importable,
else subprocess), build its episode-identification prompt against the series'
episode list (titles + overviews from Sonarr context), call the configured
OpenAI-compatible endpoint (base_url/model/api_key from plugin config;
Ollama works via base_url). LLM must return structured JSON `{season,
episodes, confidence, reasoning}` (request `response_format` json_object;
one retry on parse failure). Map to candidates: identified ident with LLM
confidence; always include claimed-ident candidate (confidence = LLM's if it
identified the claimed ep, else `1 - confidence` floor 0). No embedded subs →
abstain; LLM/API failure after retry → error.

- [ ] Failing tests: respx-mocked LLM endpoint → candidates for match and
  mislabel cases; malformed-JSON retry then error; abstain when no sub
  assets; prompt contains episode titles from context.
- [ ] Implement; verify; commit `feat(plugin): subs-llm identifier`.

### Task 13: Remediation

**Files:** Create `src/impostarr/remediate.py`, `tests/test_remediate.py`.

`Remediator(client, instance_cfg, session)`:
- `replace(job)` — per spec mechanics: if grab history captured →
  `mark_history_failed` (blocklist) else record "unblocklistable"; then
  `delete_episode_file`; then `command EpisodeSearch(episode_ids)`. Each step
  appended to `verdicts.remediation_log` (step, ok, response/err, ts) before
  the next runs; a step failure stops the sequence and routes the job to
  `quarantine` with the log.
- `remap(job, target_episode_ids)` — spec choreography: hardlink (fallback
  copy) into `staging_dir` under corrected `SxxEyy` name → delete original
  episodeFile via API → `manual_import_candidates(staging_dir)` → select the
  staged file, set episode ids → `execute_manual_import(importMode="move")`.
  Failure at any point after the hardlink leaves the staging file and routes
  to `quarantine` with the log and staging path. Occupied target (any target
  episode `hasFile`) → never called automatically (routing guarantees this);
  method still refuses + quarantines as defense in depth.

- [ ] Failing tests (respx + tmp_path fs with real hardlinks): replace with
  and without grab history; step-failure mid-sequence → quarantine + partial
  log preserved; remap happy path (assert staged link exists, API call order,
  final log); remap API failure → staging file retained; occupied-target
  refusal.
- [ ] Implement; verify; commit `feat(remediate): replace + remap choreography`.

### Task 14: Worker pipeline

**Files:** Create `src/impostarr/worker.py`, `src/impostarr/pipeline.py`,
`tests/test_pipeline.py`.

`pipeline.py` — `process_job(job, deps) -> None`: load file + series context
from Sonarr (cached per series per run) → ensure assets (skip extraction
stages whose fingerprint matches cache) → run enabled plugins (fingerprint
check per plugin; reuse cached `plugin_results` on match) → normalize →
aggregate → route → apply: matched/quarantine/inconclusive set job status +
verdict row; remediate outcomes call Remediator (auto) or store
`proposed_action` + quarantine (proposed). Phash corpus write per spec
gating. Dupe check: after frame hashes stored, compare against local corpus
(`hamming_similarity ≥ 0.9`) and attach dupe info to verdict evidence.

`worker.py` — `WorkerPool(n, deps)`: asyncio tasks looping
claim→heartbeat→process→release with reap sweep and per-instance discovery
scheduling (poll_interval per instance); graceful shutdown.

- [ ] Failing tests: end-to-end with fake plugins/transcriber/respx-Sonarr —
  one job each landing in matched / quarantine / inconclusive / auto-replace
  / proposed-remap; cached plugin_results reused on second run (assert plugin
  called once); stale lease reaped.
- [ ] Implement; verify; commit `feat(worker): pipeline + pool`.

### Task 15: HTTP API + auth + SSE

**Files:** Create `src/impostarr/api/__init__.py`,
`src/impostarr/api/auth.py`, `src/impostarr/api/routes.py`,
`src/impostarr/api/events.py`, `src/impostarr/main.py`,
`tests/api/test_auth.py`, `tests/api/test_routes.py`.

`auth.py` — middleware per spec: identity from trusted header if configured,
else API key match, else `anon`; group gate 403 only when configured;
identity attached to request state and audit-logged on mutating routes.

`routes.py` (all JSON, `/api/v1`):
- `GET /healthz` (no auth attribution needed), `GET /status` (instances,
  worker counts, queue depths).
- `GET /queues/{status}?page=` — paged job summaries.
- `GET /jobs/{id}` — full detail: file + Sonarr idents, per-plugin results,
  score sheet, verdict, remediation log, asset refs.
- `GET /jobs/{id}/assets/{asset_id}` — serves thumbnails/transcript excerpts.
- `POST /jobs/{id}/verdict {verdict: is_claimed|is_other|ignore, ident?}` —
  human verdict; `is_other` with ident triggers optional remap approval.
- `POST /jobs/{id}/approve` / `POST /jobs/{id}/reject` — act on proposed action.
- `POST /jobs/{id}/park`, `POST /jobs/{id}/unpark`, `POST /jobs/{id}/rerun`.
- `POST /instances/{name}/backfill {batch_size}`.
- `GET /events` — SSE stream of `{type: job_update, job_id, status}` +
  periodic `stats` events, fed by an in-process pub/sub in `events.py`.

`main.py` — app assembly: settings, db init, plugin load, worker pool
lifespan, static file serving of `web/dist` at `/`.

- [ ] Failing tests: anon-is-admin; group gate 403; api-key identity; each
  route happy path + 404s; verdict writes gold record; approve executes via
  mocked Remediator; SSE yields job_update on pub.
- [ ] Implement; verify; commit `feat(api): REST + SSE + auth`.

### Task 16: Frontend scaffold + API layer

**Files:** Create `web/` via Vite react-ts template; add Tailwind + Headless
UI; `web/src/api/client.ts` (typed fetch wrappers mirroring Task 15 routes),
`web/src/api/types.ts` (TS mirrors of job/verdict/score types),
`web/src/api/sse.ts` (EventSource hook with reconnect).

- [ ] Vitest tests for the SSE hook (mock EventSource: events dispatch state
  updates, reconnect on error) and client error handling (non-2xx → typed
  error).
- [ ] Dark theme base styling in the Jellyseerr vein (slate/indigo Tailwind
  palette), app shell with status header placeholder.
- [ ] Verify: `npm run build` succeeds; `npm test` passes; FastAPI serves
  `web/dist` (manual curl check documented in README dev section).
- [ ] Commit `feat(web): scaffold + typed API layer`.

### Task 17: Queue UI + inspect modal

**Files:** Create `web/src/components/QueueTabs.tsx`, `QueueTable.tsx`,
`StatusHeader.tsx`, `InspectModal.tsx`, `VerdictActions.tsx`,
`web/src/App.tsx` wiring.

Single-screen layout per spec UI section: status header (instance/worker/
throughput from `/status`), Headless-UI `Tab.Group` over the seven queues
(counts badge per tab, live via SSE), table rows (series, SxxEyy, score
badge color-coded by band, age, instance). Row click → `InspectModal`
(Headless-UI Dialog): claimed mapping + external ids, per-plugin candidate
table with status/reason/evidence, transcript excerpt, framegrab strip
(lazy-loaded thumbnails), ffprobe summary, remediation log; `VerdictActions`
per state (verdict buttons on quarantine/inconclusive; approve/reject on
proposed; park/unpark/rerun elsewhere) calling Task 16 client with
optimistic update + SSE reconciliation.

- [ ] Vitest component tests: tabs render with counts; SSE event moves a job
  between tabs; modal renders a canned full job detail; verdict button fires
  correct API call and disables while pending.
- [ ] Verify build + tests; commit `feat(web): queue tabs + inspect modal`.

### Task 18: Packaging and deployment

**Files:** Create `docker/Dockerfile` (multi-stage: web build → uv python
build → runtime with ffmpeg/mkvtoolnix/tesseract; non-root; volumes
`/config /assets /models /media`; entrypoint runs plugin ensure-install then
uvicorn), `docker/Dockerfile.cuda` (FROM nvidia cuda runtime base matching
pinned ctranslate2; otherwise identical), `docker/compose.yml` (impostarr +
optional postgres profile + all four volumes), `docker/compose.example.env`,
`.dockerignore`; modify `README.md` (quickstart, volume/backup notes per
spec, config reference pointer, GPU notes).

- [ ] Verify: `docker build` (CPU image) succeeds locally; container starts
  with example config against a mock-free environment (healthz 200, UI
  served) using `docker run` smoke script `docker/smoke.sh` (build, run with
  tmp volumes, curl /healthz + /, exit non-zero on failure).
- [ ] Commit `feat(deploy): docker images + compose`.

### Task 19: Close-out

- [ ] Full suite: `uv run pytest -v`, `uv run ruff check .`, `npm test`,
  `npm run build`, `docker/smoke.sh` — all green.
- [ ] README final pass: purpose, architecture sketch, quickstart, config
  reference, dev guide (uv, mise, test commands), spec/plan links.
- [ ] Reconcile spec vs implementation; note any deviations in the spec's
  Status section.
- [ ] Merge `poc` → `main`, delete branch.

---

## Self-review notes (author)

- Spec coverage: every spec section maps to ≥1 task (config→2, data
  model→3, discovery→6, assets/refsubs→7–8, contract/normalize→9,
  scoring→10, plugins→11–12, remediation→13, pipeline/flywheel/dupes→14,
  auth/API/UI→15–17, deployment→18). Public-phash-DB, remote workers,
  Radarr: out of scope per spec.
- Deliberately deferred to implementer judgment: exact pydantic model file
  splits, Alembic revision naming, Tailwind class details.
- Type names used across tasks: `PluginResult`, `Candidate`,
  `CandidateIdent`, `NormalizedCandidate`, `ScoreSheet`, `RoutingDecision`,
  `Remediator`, `RefSubService`, `Transcriber` — consistent as written.
