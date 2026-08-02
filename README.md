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

## GPU (whisper transcription)

The `whisper` extra (`faster-whisper` + `ctranslate2`) is always bundled in
the image — transcription-based identifier plugins are never dead weight,
they just run slower without a GPU. By default, transcription runs on CPU;
no extra setup needed.

To use an NVIDIA GPU instead: install the
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host, run the container with `--gpus all` (or the compose
equivalent, `deploy.resources.reservations.devices` with driver `nvidia`),
and set `workers.whisper_device: cuda` (or `auto`) in `impostarr.yml`.
`ctranslate2`'s GPU wheels bundle the CUDA runtime libraries they need, so
no separate CUDA base image is required.

Non-NVIDIA GPUs (e.g. Intel iGPUs) are **not** accelerated currently — the
CPU path is used regardless. Adding a backend for them (OpenVINO,
whisper.cpp, etc.) would slot in at the `Transcriber` interface
(`src/impostarr/assets/transcribe.py`), alongside `FasterWhisperTranscriber`.

## Plugins

Identifier plugins are the pluggable comparison engines behind Impostarr's
verification: each one inspects a file's extracted assets (transcript,
embedded subtitles, frame hashes, ...) and returns candidate episode
identities with confidence scores, which are combined into the file's
overall verification score. Plugins are discovered via a Python entry-point
group, configured per-plugin in `impostarr.yml` under `plugins.identifiers`.

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
| `base_url` | `https://api.openai.com/v1` | OpenAI-compatible endpoint; point this at a local [Ollama](https://ollama.com/) instance to run without a paid API. |
| `model` | `gpt-4o-mini` | Model name passed to the endpoint. |
| `api_key` | `""` | Bearer token; leave empty for endpoints that don't require one (e.g. local Ollama). |
| `max_cues` | 80 | Max subtitle cues sent per request. |
| `timeout_s` | 60 | HTTP request timeout. |

Needs embedded text subtitles in the media file (image-based subs like
PGS/VobSub aren't supported) and an OpenAI-compatible endpoint — Ollama
works, and needs no API key.

### Writing a plugin

Plugins are discovered via the `impostarr.identifiers` entry-point group.
Subclass `IdentifierPlugin` (`src/impostarr/plugins/base.py`), set `name`,
`version`, and optionally `config_model` (a pydantic `BaseModel` — its
fields become the `options` block in `impostarr.yml`), and implement:

```python
async def identify(self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult: ...
```

Return a `PluginResult` with `status`: `"ok"` with `candidates` (must
include at least one candidate with `ident.series == "claimed"` — this is
how a plugin votes "yes, this is what it's labelled"), `"abstain"` with a
`reason` when the plugin has nothing to go on (e.g. no transcript), or
`"error"` with a `reason` on failure — never raise out of `identify`.

Register the entry point in your plugin package's `pyproject.toml`:

```toml
[project.entry-points."impostarr.identifiers"]
my-plugin = "my_package.my_plugin:MyPlugin"
```

Install it by adding a pinned pip spec to `plugins.sources` in
`impostarr.yml` (e.g. `git+https://...@v1.0#subdirectory=my-plugin`) —
Impostarr installs it into a dedicated venv overlay at container boot.

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
