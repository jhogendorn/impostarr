"""Identifier plugin discovery/loading and the boot-time external installer.

`load_plugins` discovers `IdentifierPlugin` subclasses via the
`impostarr.identifiers` entry-point group, applies per-plugin config from
`Settings.plugins.identifiers` (keyed by entry point name), and skips
disabled plugins. A single plugin's discovery/instantiation failure (import
error, bad class, bad options) is logged and skipped, never crashes the
loader.

`ensure_external_plugins` installs pinned external plugin distributions
(`Settings.plugins.sources`) into the running environment via `uv pip
install`, gated on a lock-hash of the sorted spec list so unchanged specs
are a no-op on every boot after the first.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings
from .base import IdentifierPlugin

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "impostarr.identifiers"
LOCK_FILENAME = "plugins.lock"


@dataclass
class LoadedPlugin:
    plugin: IdentifierPlugin
    weight: float
    config: BaseModel | None


def load_plugins(settings: Settings) -> list[LoadedPlugin]:
    loaded: list[LoadedPlugin] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
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
            plugin = plugin_cls()
        except Exception:
            logger.exception("failed to instantiate identifier plugin %r", ep.name)
            continue

        loaded.append(LoadedPlugin(plugin=plugin, weight=weight, config=config))

    return loaded


def ensure_external_plugins(specs: list[str], state_dir: Path) -> None:
    """Install pinned external plugin specs, skipping when unchanged.

    Lock hash is sha256 over the sorted spec list, stored at
    `<state_dir>/plugins.lock`. On install failure, logs and leaves the
    lock file untouched so the app continues without the plugin(s) rather
    than crashing or retrying every boot.
    """
    lock_path = state_dir / LOCK_FILENAME
    digest = hashlib.sha256("\n".join(sorted(specs)).encode()).hexdigest()

    if lock_path.exists() and lock_path.read_text().strip() == digest:
        return

    result = subprocess.run(["uv", "pip", "install", *specs], check=False)
    if result.returncode != 0:
        logger.error("uv pip install failed for external plugin specs: %s", specs)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(digest)
