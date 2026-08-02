from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from impostarr.assets.transcribe import FasterWhisperTranscriber, NullTranscriber


async def test_null_transcriber_returns_empty_result() -> None:
    result = await NullTranscriber().transcribe(Path("unused.wav"))
    assert result.segments == []
    assert result.language == ""


def test_faster_whisper_transcriber_raises_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "faster_whisper":
            raise ImportError("no module named faster_whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
        FasterWhisperTranscriber(model_name="base")
