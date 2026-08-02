from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _skip_external_plugin_install(monkeypatch):
    """`create_app` always calls `ensure_external_plugins`, which — even
    with an empty `plugins.sources` list (every test `Settings` here) —
    still shells out to `uv venv` + `uv pip install` per call, adding real
    wall-clock time for no effect (no specs to install). No-op it for this
    package's tests; nothing under test exercises plugin installation."""
    monkeypatch.setattr("impostarr.main.ensure_external_plugins", lambda *a, **k: None)
