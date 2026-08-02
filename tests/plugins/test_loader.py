from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from impostarr.config import PluginConfig, Settings
from impostarr.plugins import loader
from tests.plugins.fake_plugin import BrokenFakePlugin, FakePlugin


@dataclass
class FakeEntryPoint:
    name: str
    _target: Any

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _patch_entry_points(monkeypatch, entry_points: list[FakeEntryPoint]) -> None:
    monkeypatch.setattr(loader, "entry_points", lambda group=None: entry_points)


def test_load_plugins_discovers_via_entry_point(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", FakePlugin)])
    settings = Settings()

    loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert isinstance(loaded[0].plugin, FakePlugin)
    assert loaded[0].weight == 1.0
    assert loaded[0].config.confidence == 0.9  # FakePluginConfig default


def test_disabled_plugin_skipped(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", FakePlugin)])
    settings = Settings(plugins={"identifiers": {"fake": PluginConfig(enabled=False)}})

    loaded = loader.load_plugins(settings)

    assert loaded == []


def test_weight_and_options_applied(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", FakePlugin)])
    settings = Settings(
        plugins={
            "identifiers": {
                "fake": PluginConfig(weight=2.5, options={"confidence": 0.3}),
            }
        }
    )

    loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert loaded[0].weight == 2.5
    assert loaded[0].config.confidence == 0.3


def test_loader_survives_entry_point_load_error(monkeypatch, caplog):
    _patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("broken", ImportError("no such module")),
            FakeEntryPoint("fake", FakePlugin),
        ],
    )
    settings = Settings()

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.loader"):
        loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert isinstance(loaded[0].plugin, FakePlugin)
    assert any("broken" in record.message for record in caplog.records)


def test_loader_survives_instantiation_error(monkeypatch, caplog):
    _patch_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("broken", BrokenFakePlugin),
            FakeEntryPoint("fake", FakePlugin),
        ],
    )
    settings = Settings()

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.loader"):
        loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert isinstance(loaded[0].plugin, FakePlugin)


# -- ensure_external_plugins ------------------------------------------------


def _hash(specs: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(specs)).encode()).hexdigest()


def test_ensure_external_plugins_noop_on_unchanged_hash(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0", "pkg-b==2.0"]
    (tmp_path / "plugins.lock").write_text(_hash(specs))

    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess should not be called when hash is unchanged")

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path)

    assert called is False


def test_ensure_external_plugins_installs_and_writes_lock_on_change(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path)

    assert len(calls) == 1
    assert calls[0][:3] == ["uv", "pip", "install"]
    assert "pkg-a==1.0" in calls[0]
    lock_path = tmp_path / "plugins.lock"
    assert lock_path.exists()
    assert lock_path.read_text().strip() == _hash(specs)


def test_ensure_external_plugins_failure_does_not_write_lock_or_raise(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(loader.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    loader.ensure_external_plugins(specs, tmp_path)  # must not raise

    assert not (tmp_path / "plugins.lock").exists()
