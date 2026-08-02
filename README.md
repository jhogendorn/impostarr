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
inconclusive / auto-remediate (remap or replace via Sonarr's API). See the
design spec for the full model:
[docs/specs/2026-08-02-impostarr-poc-design.md](docs/specs/2026-08-02-impostarr-poc-design.md).
Implementation is tracked task-by-task in
[docs/plans/2026-08-02-impostarr-poc.md](docs/plans/2026-08-02-impostarr-poc.md).

## Quickstart (docker compose)

```sh
cd docker
cp compose.example.env compose.env   # fill in MEDIA_PATH, etc.
mkdir -p config assets models
sudo chown -R 1000:1000 config assets models   # match the container's default uid/gid
cp ../examples/impostarr.yml config/impostarr.yml   # edit: sonarr instances, plugin keys, ...
docker compose --env-file compose.env up -d
```

The UI/API is then at `http://localhost:8484`. Health check:
`curl http://localhost:8484/api/v1/healthz`.

### Container user (PUID/PGID)

The image starts as root and immediately drops privileges
(`docker/entrypoint.sh`, via `gosu`) to a built-in `impostarr` user, uid/gid
1000 by default — the app itself never runs as root. If your host uid/gid
isn't 1000, either `chown` `./config`, `./assets`, `./models` to 1000:1000
(as above), or set `PUID`/`PGID` in `compose.env` to your host uid/gid
instead — the container remaps `impostarr` to match before dropping
privileges, so it can write to directories owned by any uid/gid without a
`chown`.

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

SQLite (the default) implies a single replica. For multiple replicas, set
`db.dsn` to a Postgres connection string (`postgresql+psycopg://...`) and
run the `postgres` compose profile — see the comment block in
[docker/compose.yml](docker/compose.yml). Both docker images include the
`postgres` extra (psycopg) already; outside Docker, install it with
`uv sync --extra postgres`.

## GPU (whisper transcription)

The default CPU image (`docker/Dockerfile`) does not bundle `faster-whisper`
— without it, transcription-based plugins fall back to a no-op transcriber.
`docker/Dockerfile.cuda` builds the same image with the `whisper` extra
(`faster-whisper` + `ctranslate2`, whose GPU wheels bundle the CUDA runtime
libraries they need — no separate CUDA base image required). To use it:

```sh
docker build -f docker/Dockerfile.cuda -t impostarr:cuda .
docker run --gpus all ... impostarr:cuda
```

Requires the [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host, and `workers.whisper_device: cuda` (or `auto`) in
`impostarr.yml`.

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
- Docker image smoke test (builds the CPU image, runs it with throwaway
  volumes and a minimal config, checks `/api/v1/healthz` and the UI):
  ```sh
  bash docker/smoke.sh
  ```
