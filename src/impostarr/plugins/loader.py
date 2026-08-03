"""Identifier plugin discovery/loading and the boot-time external installer.

`load_plugins` discovers `IdentifierPlugin` subclasses via the
`impostarr.identifiers` entry-point group, applies per-plugin config from
`Settings.plugins.identifiers` (keyed by entry point name), and skips
disabled plugins. When a plugin declares `config_model`, its options are
validated into that model and passed to the constructor as `plugin_cls(config=
validated_config)`, landing on `IdentifierPlugin.config`; plugins with no
`config_model` are instantiated bare (`plugin_cls()`). A single plugin's
discovery/instantiation failure (import error, bad class, bad options, wrong
type) is logged and skipped, never crashes the loader. Configured identifier
names with no matching discovered entry point are logged as a warning
(likely a typo or an external plugin that failed to install).

`ensure_external_plugins` installs pinned external plugin distributions
(`Settings.plugins.sources`) into a dedicated venv overlay (spec: "installed
at container boot into a dedicated venv overlay persisted under
`/config/plugins/venv`"), gated on a lock-hash of the sorted spec list so
unchanged specs are a no-op on every boot after the first. `uv venv` creates
the overlay if missing; `uv pip install --python <overlay>/bin/python`
targets it explicitly rather than the app's own environment.

`activate_plugin_overlay` puts that overlay's site-packages on `sys.path`
so entry points installed into it become discoverable. It is intentionally
NOT called by `load_plugins` — wiring extraction/discovery order is the
composition root's job (`main.py`), which must call it before
`load_plugins` at startup.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings
from .base import IdentifierPlugin

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "impostarr.identifiers"
LOCK_FILENAME = "plugins.lock"
UV_TIMEOUT_S = 300


@dataclass
class LoadedPlugin:
    plugin: IdentifierPlugin
    weight: float
    config: BaseModel | None
    # Max plugin EXECUTIONS per UTC day (PluginConfig.daily_budget); None =
    # unlimited. Enforced by pipeline.py's `_run_plugin_stage`, not here --
    # the loader only threads the configured value through.
    daily_budget: int | None = None


def load_plugins(settings: Settings) -> list[LoadedPlugin]:
    loaded: list[LoadedPlugin] = []
    eps = list(entry_points(group=ENTRY_POINT_GROUP))
    discovered_names = {ep.name for ep in eps}

    for ep in eps:
        try:
            plugin_cls = ep.load()
        except Exception:
            logger.exception("failed to load identifier plugin entry point %r", ep.name)
            continue

        plugin_cfg = settings.plugins.identifiers.get(ep.name)
        if plugin_cfg is not None and not plugin_cfg.enabled:
            continue
        weight = plugin_cfg.weight if plugin_cfg is not None else 1.0
        options = plugin_cfg.options if plugin_cfg is not None else {}

        try:
            config_model = getattr(plugin_cls, "config_model", None)
            config = config_model(**options) if config_model is not None else None
            plugin = plugin_cls(config=config) if config_model is not None else plugin_cls()
        except Exception:
            logger.exception("failed to instantiate identifier plugin %r", ep.name)
            continue

        if not isinstance(plugin, IdentifierPlugin):
            logger.error(
                "entry point %r did not produce an IdentifierPlugin instance: %r", ep.name, plugin
            )
            continue

        daily_budget = plugin_cfg.daily_budget if plugin_cfg is not None else None
        loaded.append(LoadedPlugin(plugin=plugin, weight=weight, config=config, daily_budget=daily_budget))

    for name in settings.plugins.identifiers:
        if name not in discovered_names:
            logger.warning("configured identifier plugin %r has no matching entry point", name)

    return loaded


def _run_uv(cmd: list[str]) -> bool:
    """Run a `uv` subprocess; True on rc==0, False (logged) on nonzero rc or timeout."""
    try:
        result = subprocess.run(cmd, check=False, timeout=UV_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.error("uv command timed out after %ss: %s", UV_TIMEOUT_S, cmd)
        return False
    if result.returncode != 0:
        logger.error("uv command failed (rc=%s): %s", result.returncode, cmd)
        return False
    return True


def ensure_external_plugins(specs: list[str], state_dir: Path, venv_dir: Path) -> None:
    """Install pinned external plugin specs into the venv overlay, skipping
    when unchanged.

    Lock hash is sha256 over the sorted spec list, stored at
    `<state_dir>/plugins.lock`. On failure (nonzero rc or timeout) at either
    step, logs and leaves the lock file untouched so the app continues
    without the plugin(s) rather than crashing or retrying every boot.
    """
    if not specs:
        return

    lock_path = state_dir / LOCK_FILENAME
    digest = hashlib.sha256("\n".join(sorted(specs)).encode()).hexdigest()

    if lock_path.exists() and lock_path.read_text().strip() == digest:
        return

    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists() and not _run_uv(["uv", "venv", str(venv_dir)]):
        return

    if not _run_uv(["uv", "pip", "install", "--python", str(venv_python), *specs]):
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(digest)


def activate_plugin_overlay(venv_dir: Path) -> None:
    """Append the plugin venv overlay's site-packages dir to `sys.path`.

    The pythonX.Y segment is discovered by globbing rather than hardcoded,
    since the overlay's interpreter version isn't otherwise known here. A
    no-op when the overlay (or its site-packages dir) doesn't exist yet —
    e.g. no external plugins configured, or install hasn't run.
    """
    matches = sorted(venv_dir.glob("lib/python3.*/site-packages"))
    if not matches:
        return
    site_packages = str(matches[-1])
    if site_packages not in sys.path:
        sys.path.append(site_packages)
