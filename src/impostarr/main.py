"""Composition root: builds the FastAPI app from `Settings`.

Recommended entrypoint is `uvicorn --factory impostarr.main:create_app`
(or the `app` factory alias below) rather than a module-level `app =
create_app()` — the latter would run settings/DB/plugin loading as an
import-time side effect, which is exactly what tests construct their own
`Settings` to avoid.

Known PoC gap: plugins themselves are constructed by
`plugins.loader.load_plugins` without any per-plugin dependency injection
from this module — a plugin like `subs-llm` that wants its own HTTP client
builds one internally rather than receiving one from here. That's
independent of `RefSubService` below: refsubs is a shared service *this*
module constructs and threads through `PipelineDeps.refsubs` (plugins read
it off `ctx.refsubs`, e.g. `whisper-subs`), not something the loader
injects into a plugin's constructor. Documented rather than "fixed" here —
reworking the loader's construction protocol is out of scope for this
task.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from impostarr.api.auth import AuthMiddleware
from impostarr.api.events import EventBus
from impostarr.api.logbuffer import RingBufferHandler
from impostarr.api.routes import router
from impostarr.config import Settings, SonarrInstance, load_settings
from impostarr.db import init_db, make_session_factory
from impostarr.discovery import Discoverer
from impostarr.pipeline import PipelineDeps
from impostarr.plugins.loader import activate_plugin_overlay, ensure_external_plugins, load_plugins
from impostarr.plugins.transcribers import load_transcriber
from impostarr.refsubs import RefSubService
from impostarr.sonarr import SonarrClient
from impostarr.worker import WorkerPool

logger = logging.getLogger(__name__)


@dataclass
class InstanceRuntime:
    """Per-Sonarr-instance runtime handles that routes look up via
    `app.state.instances[name]`."""

    client: SonarrClient
    cfg: SonarrInstance
    discoverer: Discoverer


class CacheAwareStaticFiles(StaticFiles):
    """StaticFiles with explicit cache semantics for a hashed-asset SPA.

    Vite emits content-hashed filenames under assets/ (safe to cache forever);
    index.html is the un-hashed entrypoint that references them. Without an
    explicit Cache-Control, browsers heuristically cache index.html and can
    keep serving a stale HTML+bundle pair after the container is rebuilt.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        path = getattr(response, "path", "") or ""
        if "/assets/" in str(path).replace("\\", "/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _resolve_web_dist() -> Path:
    """Locate `web/dist` (Task 16's frontend build output).

    Anchored to this source file's location first — `<repo>/web/dist`,
    computed from `main.py`'s own path (`src/impostarr/main.py`, so the
    repo root is two parents up from the package dir) — so it resolves
    correctly regardless of the process's current working directory (e.g.
    uvicorn launched from an arbitrary cwd during dev). Falls back to
    `./web/dist` relative to cwd for layouts where that anchor doesn't
    hold — e.g. an installed wheel's `main.py` living under
    `site-packages/impostarr/`, where `parents[2]` isn't the repo root at
    all; the container image (Task 18) is expected to set its cwd to
    wherever it copied `web/dist`, matching this fallback.
    """
    anchored = Path(__file__).resolve().parents[2] / "web" / "dist"
    if anchored.is_dir():
        return anchored
    return Path("web/dist")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()

    engine = init_db(settings)
    session_factory = make_session_factory(engine)

    # Log viewer (Task: dry-run + log viewer feedback batch): captures the
    # last 1000 records from every impostarr.* module logger for the
    # /api/v1/logs endpoint. The "impostarr" logger has no explicit level by
    # default (falls back to root's WARNING), so INFO must be set here or
    # INFO-level records (including "DRY-RUN: ..." lines) would never reach
    # the handler at all.
    log_buffer = RingBufferHandler()
    impostarr_logger = logging.getLogger("impostarr")
    impostarr_logger.setLevel(logging.INFO)
    impostarr_logger.addHandler(log_buffer)

    plugin_venv_dir = settings.state_dir / "plugins" / "venv"
    ensure_external_plugins(settings.plugins.sources, settings.state_dir, plugin_venv_dir)
    activate_plugin_overlay(plugin_venv_dir)
    loaded_plugins = load_plugins(settings)

    transcriber = load_transcriber(settings)
    event_bus = EventBus()

    # RefSubService is instance-agnostic (no per-Sonarr-instance state), so
    # one shared instance + one shared httpx.AsyncClient serves every
    # instance's PipelineDeps below. cache_dir defaults to a subdirectory
    # under state_dir when unset in config, so the service actually caches
    # (and can therefore serve requests) out of the box rather than staying
    # permanently disabled (RefSubService.get() logs+returns None with no
    # cache_dir at all — see its docstring); manual_dir staying unset is a
    # legitimate "no manual overrides configured" state and is left as-is.
    refsubs_cfg = settings.refsubs
    if refsubs_cfg.cache_dir is None:
        refsubs_cfg = refsubs_cfg.model_copy(
            update={"cache_dir": str(settings.state_dir / "refsubs_cache")}
        )
    refsubs_http_client = httpx.AsyncClient()
    refsub_service = RefSubService(refsubs_cfg, refsubs_http_client)

    instances: dict[str, InstanceRuntime] = {}
    deps_per_instance: dict[str, PipelineDeps] = {}
    for instance_cfg in settings.sonarr:
        client = SonarrClient(instance_cfg.url, instance_cfg.api_key, dry_run=settings.dry_run)
        discoverer = Discoverer(instance_cfg, client, session_factory)
        instances[instance_cfg.name] = InstanceRuntime(client=client, cfg=instance_cfg, discoverer=discoverer)
        deps_per_instance[instance_cfg.name] = PipelineDeps(
            session_factory=session_factory,
            sonarr_client=client,
            settings=settings,
            instance_cfg=instance_cfg,
            plugins=loaded_plugins,
            transcriber=transcriber,
            refsubs=refsub_service,
            worker_id=f"pool-{instance_cfg.name}",
            event_bus=event_bus,
        )

    worker_pool = (
        WorkerPool(
            deps_per_instance,
            {name: runtime.discoverer for name, runtime in instances.items()},
            settings.workers.pool_size,
        )
        if deps_per_instance
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if worker_pool is not None:
            await worker_pool.start()
        try:
            yield
        finally:
            if worker_pool is not None:
                await worker_pool.stop()
            for runtime in instances.values():
                await runtime.client.close()
            await refsubs_http_client.aclose()
            impostarr_logger.removeHandler(log_buffer)

    fastapi_app = FastAPI(lifespan=lifespan)
    fastapi_app.state.settings = settings
    fastapi_app.state.session_factory = session_factory
    fastapi_app.state.event_bus = event_bus
    fastapi_app.state.instances = instances
    fastapi_app.state.deps_per_instance = deps_per_instance
    fastapi_app.state.pool_size = settings.workers.pool_size if deps_per_instance else 0
    fastapi_app.state.log_buffer = log_buffer

    fastapi_app.add_middleware(AuthMiddleware, settings=settings)
    fastapi_app.include_router(router)

    web_dist = _resolve_web_dist()
    if web_dist.is_dir():
        fastapi_app.mount("/", CacheAwareStaticFiles(directory=web_dist, html=True), name="static")

    return fastapi_app


def app() -> FastAPI:
    """Factory alias for `uvicorn impostarr.main:app --factory`."""
    return create_app()
