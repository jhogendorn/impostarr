from __future__ import annotations

import builtins
import threading
from pathlib import Path

import pytest

from impostarr.assets.transcribe import FasterWhisperTranscriber, NullTranscriber


async def test_null_transcriber_returns_empty_result() -> None:
    result = await NullTranscriber().transcribe(Path("unused.wav"))
    assert result.segments == []
    assert result.language == ""


class _FakeInfo:
    language = "en"


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeModel:
    """Records the thread its lazy segment generator is iterated on, so the
    test can prove materialization happens off the event-loop thread."""

    def __init__(self, record_thread: list[threading.Thread]) -> None:
        self._record_thread = record_thread

    def transcribe(self, wav_path: str) -> tuple[object, _FakeInfo]:
        def gen() -> object:
            self._record_thread.append(threading.current_thread())
            yield _FakeSegment(0.0, 1.0, "hello")
            yield _FakeSegment(1.0, 2.0, "world")

        return gen(), _FakeInfo()


async def test_transcribe_materializes_segments_off_event_loop_thread() -> None:
    iterated_on: list[threading.Thread] = []
    transcriber = object.__new__(FasterWhisperTranscriber)
    transcriber._model = _FakeModel(iterated_on)  # type: ignore[attr-defined]

    event_loop_thread = threading.current_thread()
    result = await transcriber.transcribe(Path("unused.wav"))

    assert len(iterated_on) == 1
    assert iterated_on[0] is not event_loop_thread
    assert [s.text for s in result.segments] == ["hello", "world"]
    assert result.language == "en"


def test_faster_whisper_transcriber_raises_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "faster_whisper":
            raise ImportError("no module named faster_whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
        FasterWhisperTranscriber(model_name="base")
