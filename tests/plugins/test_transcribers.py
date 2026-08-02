from __future__ import annotations

import builtins
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

import pytest

from impostarr.assets.transcribe import NullTranscriber, RemoteTranscriber
from impostarr.config import Settings
from impostarr.plugins import transcribers
from impostarr.plugins.transcribers import (
    ENTRY_POINT_GROUP,
    faster_whisper_factory,
    none_factory,
    remote_factory,
    whisper_cpp_factory,
)


@dataclass
class FakeEntryPoint:
    name: str
    _target: Any

    def load(self) -> Any:
        return self._target


def _patch_entry_points(monkeypatch, entry_points_list: list[FakeEntryPoint]) -> None:
    monkeypatch.setattr(transcribers, "entry_points", lambda group=None: entry_points_list)


def test_load_transcriber_resolves_none(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("none", none_factory)])
    settings = Settings(workers={"transcriber": "none"})

    result = transcribers.load_transcriber(settings)

    assert isinstance(result, NullTranscriber)


def test_load_transcriber_resolves_remote_with_options(monkeypatch):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("remote", remote_factory)])
    settings = Settings(
        workers={
            "transcriber": "remote",
            "transcriber_options": {"base_url": "http://gpu-box:8000/v1", "api_key": "k"},
        }
    )

    result = transcribers.load_transcriber(settings)

    assert isinstance(result, RemoteTranscriber)
    assert result.base_url == "http://gpu-box:8000/v1"
    assert result.api_key == "k"


def test_remote_factory_raises_without_base_url():
    settings = Settings(workers={"transcriber": "remote"})

    with pytest.raises(RuntimeError, match="base_url"):
        remote_factory(settings.workers, settings.models_dir)


def test_load_transcriber_unknown_name_falls_back_to_null(monkeypatch, caplog):
    _patch_entry_points(monkeypatch, [FakeEntryPoint("none", none_factory)])
    settings = Settings(workers={"transcriber": "typo-name"})

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.transcribers"):
        result = transcribers.load_transcriber(settings)

    assert isinstance(result, NullTranscriber)
    assert any("typo-name" in record.message for record in caplog.records)


def test_load_transcriber_falls_back_to_null_on_construction_error(monkeypatch, caplog):
    def _broken_factory(workers, models_dir):
        raise RuntimeError("boom")

    _patch_entry_points(monkeypatch, [FakeEntryPoint("broken", _broken_factory)])
    settings = Settings(workers={"transcriber": "broken"})

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.transcribers"):
        result = transcribers.load_transcriber(settings)

    assert isinstance(result, NullTranscriber)
    assert any("broken" in record.message for record in caplog.records)


def test_load_transcriber_degrades_gracefully_when_faster_whisper_missing(monkeypatch, caplog):
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "faster_whisper":
            raise ImportError("no module named faster_whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    _patch_entry_points(monkeypatch, [FakeEntryPoint("faster-whisper", faster_whisper_factory)])
    settings = Settings(workers={"transcriber": "faster-whisper"})

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.transcribers"):
        result = transcribers.load_transcriber(settings)

    assert isinstance(result, NullTranscriber)
    assert any("faster-whisper" in record.message for record in caplog.records)


def test_load_transcriber_degrades_gracefully_when_whisper_cpp_missing(monkeypatch, caplog):
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pywhispercpp":
            raise ImportError("no module named pywhispercpp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    _patch_entry_points(monkeypatch, [FakeEntryPoint("whisper-cpp", whisper_cpp_factory)])
    settings = Settings(workers={"transcriber": "whisper-cpp"})

    with caplog.at_level(logging.ERROR, logger="impostarr.plugins.transcribers"):
        result = transcribers.load_transcriber(settings)

    assert isinstance(result, NullTranscriber)
    assert any("whisper-cpp" in record.message for record in caplog.records)


def test_real_entry_points_contain_all_built_in_transcribers():
    """Non-mocked: reads the actual `impostarr.transcribers` entry-point
    group as installed (editable install), confirming all four built-ins
    register and resolve to their expected factory functions."""
    eps = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}

    assert set(eps) == {"faster-whisper", "whisper-cpp", "remote", "none"}
    assert eps["faster-whisper"].load() is faster_whisper_factory
    assert eps["whisper-cpp"].load() is whisper_cpp_factory
    assert eps["remote"].load() is remote_factory
    assert eps["none"].load() is none_factory
