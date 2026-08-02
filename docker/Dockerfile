# syntax=docker/dockerfile:1
#
# CPU image. See docker/Dockerfile.cuda for the GPU-transcription variant
# (same structure, adds the `whisper` extra) and docker/compose.yml for a
# runnable example. Build from the repo root:
#   docker build -f docker/Dockerfile -t impostarr:latest .

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
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra postgres

# ---- Stage 3: runtime -------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      mkvtoolnix \
      tesseract-ocr \
      curl \
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

USER impostarr
ENTRYPOINT ["/entrypoint.sh"]
