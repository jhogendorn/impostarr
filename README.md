# Impostarr

Post-import content verification for Sonarr: proves files on disk are the
episodes they claim to be, via pluggable identifier plugins and confidence
scoring.

## Architecture

Impostarr is a single Python (FastAPI) service, serving its API/UI and
running an internal worker pool that verifies newly-imported (and, on
backfill, existing) episode files against one or more Sonarr instances.
Verification runs through pluggable identifier plugins (e.g. whisper
transcript-vs-reference-subtitle matching), whose results are combined into
a weighted confidence score that routes each file to matched / quarantine /
inconclusive / auto-remediate (remap or replace via Sonarr's API).

## Quickstart (docker compose)

```sh
cd docker
cp compose.example.env compose.env   # fill in MEDIA_PATH, PUID/PGID, etc.
mkdir -p config assets models
cp ../examples/impostarr.yml config/impostarr.yml   # edit: sonarr instances, plugin keys, ...
docker compose --env-file compose.env up -d
```

The UI/API is then at `http://localhost:8484`. Health check:
`curl http://localhost:8484/api/v1/healthz`.

### Container user (PUID/PGID)

Impostarr follows the same PUID/PGID convention as Sonarr, Radarr, and the
rest of the *arr ecosystem. The image starts as root and immediately drops
privileges (`docker/entrypoint.sh`, via `gosu`) to a built-in `impostarr`
user, uid/gid 1000 by default — the app itself never runs as root. Set
`PUID`/`PGID` in `compose.env` to your host user's uid/gid (`id -u`/`id
-g`) and the container remaps `impostarr` to match before dropping
privileges, so it can write to `./config`, `./assets`, `./models` however
they're owned on the host, no `chown` needed.

Fallback: if you'd rather not set PUID/PGID, `chown` the volumes to match
the container's built-in default instead: `sudo chown -R 1000:1000 config
assets models`.

## Configuration

Single YAML file, bind-mounted at `/config/impostarr.yml` (path overridable
via `IMPOSTARR_CONFIG`). Every key is optional and documented in the
annotated reference: [examples/impostarr.yml](examples/impostarr.yml). Any
value can also be set via env var (`IMPOSTARR__SECTION__KEY`, or JSON for
list/object-valued keys) — env always wins over the file.

## Dry run

`dry_run: true` (top-level in `impostarr.yml`) is strongly recommended for
first runs against a real library: no files are touched, no Sonarr state
is changed. Every action that would otherwise mutate something is instead
logged as `DRY-RUN: would ...` — visible live via `GET /api/v1/logs`
(lines highlighted amber) and, for remediation, in the job's
`remediation_log` audit trail.

Scoping: only Sonarr API mutations (delete, search, manual import,
blocklist) and media-library filesystem operations (the hardlink/copy into
staging) are suppressed. Impostarr's own database and asset extraction
(transcripts, framegrabs, phash corpus) still run — those are impostarr's
own artifacts, not the library. Jobs still walk their full step sequence
and transition to `remediated` normally, so the queue flow is observable
end-to-end; the audit trail marks what would have happened instead of
actually happening. When active, the UI shows an amber "DRY RUN" badge in
the header.

## Volumes

| Volume    | Contents                                                        | Notes |
|-----------|------------------------------------------------------------------|-------|
| `/config` | `impostarr.yml`, SQLite DB (if no `db.dsn`), watermarks, lock-hashes, plugin venv overlay | Backup-relevant. |
| `/assets` | Extracted artifact blobs: transcripts, framegrabs, refsub cache | Backup-relevant; can grow large — mountable separately (e.g. longhorn/NFS). |
| `/models` | Whisper/LLM model caches                                        | Multi-GB; disposable — mountable separately, no backup needed. |
| `/media`  | Library mount                                                    | Read-only unless remap staging (hardlink + manual import) is used. |

## Database

**SQLite is the default — zero configuration required.** With no `db.dsn`
set, Impostarr uses a SQLite file under `/config` (`state_dir`). This is
the right choice for the common case (single instance) and needs nothing
from you.

**PostgreSQL is optional**, needed only if you run multiple Impostarr
replicas against the same database. Set `db.dsn` to a Postgres connection
string (`postgresql+psycopg://...`) and run the `postgres` compose profile
— see the comment block in [docker/compose.yml](docker/compose.yml). The
docker image includes the `postgres` extra (psycopg) already; outside
Docker, install it with `uv sync --extra postgres`.

## Auth

Impostarr does not currently have a requirement for access control: all
actions are admin actions. Users who need authentication should deploy the
app behind an auth service (e.g. [Authentik](https://goauthentik.io/));
Impostarr will attribute actions to the identity the proxy passes through
(`auth.trusted_header` in `impostarr.yml`).

## GPU (transcription backends)

Transcription is a deployment choice, not a fixed dependency — pick the
backend that fits the box this runs on via `workers.transcriber` in
`impostarr.yml` (see the annotated comments in
[examples/impostarr.yml](examples/impostarr.yml)). Every backend is always
available in the image — none is ever a dead identifier that silently
disables transcript-dependent plugins.

| Backend | Hardware | Setup |
|---|---|---|
| `faster-whisper` (default) | CPU (any); NVIDIA GPU via CUDA | Bundled, no extra setup for CPU. For CUDA: install the [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host, run the container with `--gpus all` (or the compose equivalent, `deploy.resources.reservations.devices` with driver `nvidia`), set `workers.whisper_device: cuda`. `ctranslate2`'s GPU wheels bundle the CUDA runtime libraries they need, so no separate CUDA base image is required. |
| `whisper-cpp` | CPU (any) out of the box; Intel/AMD iGPU via Vulkan | Bundled CPU wheel (`pywhispercpp`, small, no compiler needed). Vulkan iGPU acceleration needs a source build of `pywhispercpp` against a Vulkan-enabled whisper.cpp — **not built by this image**; see [pywhispercpp](https://github.com/absadiki/pywhispercpp) and [whisper.cpp](https://github.com/ggml-org/whisper.cpp)'s Vulkan build docs if you want to build your own image with it. |
| `remote` | Whatever the remote server has | Set `workers.transcriber_options.base_url` to any OpenAI-compatible `/v1/audio/transcriptions` server (e.g. [speaches](https://github.com/speaches-ai/speaches), faster-whisper-server) running on a box that actually has a GPU. **Recommended when the Impostarr box itself has no usable GPU** — e.g. an Intel iGPU with no Vulkan build available. |
| `none` | N/A | Disables transcription; transcript-dependent plugins (`whisper-subs`) abstain rather than erroring. |

`faster-whisper`/`whisper-cpp` share `workers.whisper_model` (model-size
name); `workers.whisper_device` is faster-whisper-only. `whisper-cpp` and
`remote` read their own settings from `workers.transcriber_options`.

## Plugins

Identifier plugins are the pluggable comparison engines behind Impostarr's
verification: each one inspects a file's extracted assets (transcript,
embedded subtitles, frame hashes, ...) and returns candidate episode
identities with confidence scores, which are combined into the file's
overall verification score. Plugins are discovered via a Python entry-point
group, configured per-plugin in `impostarr.yml` under `plugins.identifiers`.

Transcriber backends (see GPU section above) are also an entry-point group
(`impostarr.transcribers`), following the same discovery model but with a
factory-function convention instead of a class — see
`src/impostarr/plugins/transcribers.py`.

### whisper-subs

Compares a whisper transcript of the file's audio against reference
subtitles for nearby episodes.

| Option | Default | Meaning |
|---|---|---|
| `min_lines` | 20 | Minimum transcript segments required to attempt a comparison; shorter transcripts abstain. |
| `min_compared` | 10 | Reference-subtitle line count below which the match confidence is discounted (a near-empty reference is weak evidence even at a high match ratio). |

Needs reference subtitles to compare against: either OpenSubtitles
credentials (`refsubs.username`/`refsubs.password`/`refsubs.api_key`) for
automatic fetching, or a manual SRT drop-in directory
(`refsubs.manual_dir`, layout `<tvdb_id>/SxxEyy.srt`).

### subs-llm

Sends the file's embedded subtitle text to an OpenAI-compatible chat
endpoint and asks it which episode the cues belong to.

| Option | Default | Meaning |
|---|---|---|
| `base_url` | `https://api.openai.com/v1` | OpenAI-compatible endpoint — any OpenAI-compatible server works (e.g. [Ollama](https://ollama.com/), LocalAI, llama.cpp server, vLLM); point this at one to run without a paid API. |
| `model` | `gpt-4o-mini` | Model name passed to the endpoint. |
| `api_key` | `""` | Bearer token; leave empty for endpoints that don't require one (e.g. local Ollama). |
| `max_cues` | 80 | Max subtitle cues sent per request. |
| `timeout_s` | 60 | HTTP request timeout. |

Needs embedded text subtitles in the media file (image-based subs like
PGS/VobSub aren't supported) and an OpenAI-compatible endpoint — Ollama
works, and needs no API key.

### Writing a plugin

`whisper-subs` and `subs-llm` are themselves ordinary third-party-style
plugins bundled with Impostarr for convenience, not special-cased code: each
lives in its own package under `src/` (`src/impostarr_plugin_whisper_subs/`,
`src/impostarr_plugin_subs_llm/`), registers its own entry point, defines
its own config model, and imports only from `impostarr.plugins.*` — exactly
what an external plugin package looks like. Use either as a reference
implementation.

Plugins are discovered via the `impostarr.identifiers` entry-point group.
Subclass `IdentifierPlugin` (`impostarr.plugins.base.IdentifierPlugin`), set
`name`, `version`, and optionally `config_model` (a pydantic `BaseModel` —
its fields become the `options` block in `impostarr.yml`), and implement:

```python
async def identify(self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult: ...
```

Return a `PluginResult` with `status`: `"ok"` with `candidates` (must
include at least one candidate with `ident.series == "claimed"` — this is
how a plugin votes "yes, this is what it's labelled"), `"abstain"` with a
`reason` when the plugin has nothing to go on (e.g. no transcript), or
`"error"` with a `reason` on failure — never raise out of `identify`.

A minimal SRT cue parser is available as public API for plugins that need
one: `impostarr.plugins.subtitles.parse_srt` (used by both bundled
plugins) — no need to write your own.

Register the entry point in your plugin package's `pyproject.toml`:

```toml
[project.entry-points."impostarr.identifiers"]
my-plugin = "my_package.my_plugin:MyPlugin"
```

Install it by adding a pinned pip spec to `plugins.sources` in
`impostarr.yml` (e.g. `git+https://...@v1.0#subdirectory=my-plugin`) —
Impostarr installs it into a dedicated venv overlay at container boot.

## Demo / e2e

One command spins up a complete, throwaway Impostarr deployment against a
real Sonarr instance and a synthetic library — no copyrighted media
involved, everything is generated locally by ffmpeg:

```sh
bash demo/e2e.sh
```

What it builds: Sonarr + Impostarr (`dry_run: true`) + a stub
OpenAI-compatible transcription server, via `demo/compose.yml`. It
generates a synthetic 4-episode library (`demo/generate_media.py`, testsrc2
video + sine audio + embedded/reference subtitles — a TVDB-real series,
"Pioneer One" by default, falling back to another well-known title if
SkyHook lookup fails), seeds it into Sonarr (`demo/seed.sh`), then triggers
an Impostarr backfill and polls until every file is verified. One file
(`S01E04`) is deliberately mislabeled — it's really episode 5's content —
so the run asserts 3 files come back `matched` and 1 comes back
`remediated` with a `DRY-RUN`-prefixed remediation log proposing the
correct remap to `S01E05`.

The stack is left running afterwards for interactive inspection at
`http://localhost:8484` — open the queue, click into the remediated job,
and look at its plugin results and remediation log. Pass `--down` for CI
mode (tears the stack down after asserting).

Teardown: `(cd demo && docker compose down -v)`. Every run starts by
wiping `demo/volumes/` and rebuilding from scratch, so re-running is always
safe.

## Development

- Tool versions via [mise](https://mise.jdx.dev/) (`mise.toml`: Python 3.12, Node 24).
- Python deps/venv via [uv](https://docs.astral.sh/uv/):
  ```sh
  uv sync
  uv run pytest -v
  uv run ruff check .
  ```
- Frontend (`web/`):
  ```sh
  cd web
  npm ci
  npm test
  npm run build
  ```
- Docker image smoke test (builds the image, runs it with throwaway
  volumes and a minimal config, checks `/api/v1/healthz` and the UI):
  ```sh
  bash docker/smoke.sh
  ```
