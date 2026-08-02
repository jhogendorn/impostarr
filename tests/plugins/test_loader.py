from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from impostarr.config import PluginConfig, Settings
from impostarr.plugins import loader
from tests.plugins.fake_plugin import BrokenFakePlugin, FakePlugin, NoConfigFakePlugin


@dataclass
class FakeEntryPoint:
    name: str
    _target: Any

    def load(self) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


class NotAPlugin:
    """A class that instantiates fine but isn't an `IdentifierPlugin`."""

    name = "not-a-plugin"
    version = "0.0.0"
    config_model = None


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


def test_loader_skips_non_identifier_plugin_instance(monkeypatch, caplog):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("wrong-type", NotAPlugin)])
    settings = Settings()

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.loader"):
        loaded = loader.load_plugins(settings)

    assert loaded == []
    assert any("wrong-type" in record.message for record in caplog.records)


def test_loader_passes_validated_config_into_plugin_instance(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", FakePlugin)])
    settings = Settings(
        plugins={"identifiers": {"fake": PluginConfig(options={"confidence": 0.3})}}
    )

    loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert loaded[0].plugin.config.confidence == 0.3


def test_loader_instantiates_bare_when_no_config_model(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("no-config", NoConfigFakePlugin)])
    settings = Settings()

    loaded = loader.load_plugins(settings)

    assert len(loaded) == 1
    assert isinstance(loaded[0].plugin, NoConfigFakePlugin)
    assert loaded[0].plugin.config is None


def test_loader_warns_on_configured_name_with_no_entry_point(monkeypatch, caplog):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("fake", FakePlugin)])
    settings = Settings(plugins={"identifiers": {"typo-name": PluginConfig()}})

    with caplog.at_level(logging.WARNING, logger="impostarr.plugins.loader"):
        loader.load_plugins(settings)

    assert any("typo-name" in record.message for record in caplog.records)


# -- ensure_external_plugins ------------------------------------------------


def _hash(specs: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(specs)).encode()).hexdigest()


def _existing_venv(venv_dir):
    python = venv_dir / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    return python


def test_ensure_external_plugins_noop_on_unchanged_hash(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0", "pkg-b==2.0"]
    (tmp_path / "plugins.lock").write_text(_hash(specs))
    venv_dir = tmp_path / "venv"

    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess should not be called when hash is unchanged")

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)


def test_ensure_external_plugins_creates_venv_before_install_when_missing(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"
    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["uv", "venv"]:
            _existing_venv(venv_dir)
        return FakeResult()

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)

    assert len(calls) == 2
    venv_cmd, venv_kwargs = calls[0]
    install_cmd, install_kwargs = calls[1]
    assert venv_cmd == ["uv", "venv", str(venv_dir)]
    assert venv_kwargs["timeout"] == 300
    assert install_cmd[:3] == ["uv", "pip", "install"]
    assert install_cmd[3] == "--python"
    assert install_cmd[4] == str(venv_dir / "bin" / "python")
    assert "pkg-a==1.0" in install_cmd
    assert install_kwargs["timeout"] == 300

    lock_path = tmp_path / "plugins.lock"
    assert lock_path.read_text().strip() == _hash(specs)


def test_ensure_external_plugins_skips_venv_creation_when_present(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"
    venv_python = _existing_venv(venv_dir)
    calls = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)

    assert len(calls) == 1
    assert calls[0][:3] == ["uv", "pip", "install"]
    assert calls[0][4] == str(venv_python)
    assert (tmp_path / "plugins.lock").read_text().strip() == _hash(specs)


def test_ensure_external_plugins_venv_creation_failure_aborts(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"
    calls = []

    class FakeResult:
        returncode = 1

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)

    assert len(calls) == 1  # install never attempted
    assert not (tmp_path / "plugins.lock").exists()


def test_ensure_external_plugins_install_failure_does_not_write_lock_or_raise(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"
    _existing_venv(venv_dir)

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(loader.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)  # must not raise

    assert not (tmp_path / "plugins.lock").exists()


def test_ensure_external_plugins_install_timeout_does_not_write_lock_or_raise(tmp_path, monkeypatch):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"
    _existing_venv(venv_dir)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 300))

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)  # must not raise

    assert not (tmp_path / "plugins.lock").exists()


def test_ensure_external_plugins_venv_creation_timeout_does_not_write_lock_or_raise(
    tmp_path, monkeypatch
):
    specs = ["pkg-a==1.0"]
    venv_dir = tmp_path / "venv"  # missing -> venv creation attempted first

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 300))

    monkeypatch.setattr(loader.subprocess, "run", fake_run)

    loader.ensure_external_plugins(specs, tmp_path, venv_dir)  # must not raise

    assert not (tmp_path / "plugins.lock").exists()


# -- activate_plugin_overlay -------------------------------------------------


def test_activate_plugin_overlay_appends_existing_site_packages(tmp_path, monkeypatch):
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))

    loader.activate_plugin_overlay(venv_dir)

    assert str(site_packages) in sys.path


def test_activate_plugin_overlay_noop_when_missing(tmp_path, monkeypatch):
    venv_dir = tmp_path / "venv"  # never created
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = list(sys.path)

    loader.activate_plugin_overlay(venv_dir)

    assert sys.path == before
