# syntax=docker/dockerfile:1
#
# Single image for all deployments. See docker/compose.yml for a runnable
# example. Build from the repo root:
#   docker build -f docker/Dockerfile -t impostarr:latest .
#
# Transcription backend is a deployment choice (`workers.transcriber` in
# impostarr.yml), never a dead identifier — both the `whisper` extra
# (faster-whisper + ctranslate2) and the `whispercpp` extra (pywhispercpp,
# a small CPU-only manylinux wheel — no compiler needed) are always
# installed, so switching backends needs no image rebuild. Default
# (`faster-whisper`) runs on CPU with no extra setup; CUDA acceleration is
# opportunistic (install nvidia-container-toolkit, run with `--gpus all`,
# set `workers.whisper_device: cuda`) — ctranslate2's GPU wheels bundle the
# CUDA runtime libraries they need, so no separate CUDA base image is
# required. Non-NVIDIA GPUs (e.g. Intel iGPUs) are NOT accelerated by
# anything in this image: `whisper-cpp`'s Vulkan iGPU path requires a
# source build (not done here — see README GPU section); the `remote`
# backend is the recommended option for an Impostarr box with only a
# non-NVIDIA GPU, pointing at an OpenAI-compatible transcription server
# running wherever the GPU actually lives. See README GPU section.

# ---- Stage 1: web build ---------------------------------------------------
FROM node:24-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- Stage 2: python dependency build -------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS python-builder

# Install into a project-local venv rather than uv's default (which would
# be relative to the source tree layout); --no-editable makes the venv
# self-contained (project installed as a real package) so it survives being
# copied into the runtime stage without src/ needing to be a sibling.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer: only pyproject.toml/uv.lock invalidate this, so editing
# src/ doesn't force re-resolving/re-downloading dependencies on rebuild.
# The cache mount persists uv's wheel cache across builds (BuildKit-only).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra whisper --extra whispercpp --extra postgres

# Project layer: install the project itself as a real (non-editable)
# package now that src/ is present.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra whisper --extra whispercpp --extra postgres

# ---- Stage 3: runtime -------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      mkvtoolnix \
      tesseract-ocr \
      curl \
      gosu \
    && rm -rf /var/lib/apt/lists/*

# `uv` binary is needed at boot: create_app() -> ensure_external_plugins()
# shells out to `uv venv` / `uv pip install` to build the pinned external
# identifier-plugin overlay under /config/plugins/venv.
COPY --from=python-builder /usr/local/bin/uv /usr/local/bin/uv

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin impostarr

WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY src ./src
# _resolve_web_dist() in main.py falls back to ./web/dist relative to cwd
# when it isn't anchored two parents above main.py (which isn't the case
# here: --no-editable installs the package into site-packages, not src/).
COPY --from=web /web/dist ./web/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R impostarr:impostarr /app

ENV PATH="/app/.venv/bin:${PATH}" \
    IMPOSTARR_CONFIG=/config/impostarr.yml \
    UV_NO_CACHE=1

VOLUME ["/config", "/assets", "/models", "/media"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8484/api/v1/healthz || exit 1

# Stays root: entrypoint.sh always drops to `impostarr` (optionally
# remapped to PUID/PGID first) via gosu before exec'ing the app — the app
# itself never runs as root. See docker/entrypoint.sh and README.
ENTRYPOINT ["/entrypoint.sh"]
