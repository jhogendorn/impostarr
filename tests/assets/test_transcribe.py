from __future__ import annotations

import builtins
import threading
from pathlib import Path

import httpx
import pytest
import respx

from impostarr.assets.transcribe import (
    FasterWhisperTranscriber,
    NullTranscriber,
    RemoteTranscriber,
    TranscribeError,
    WhisperCppTranscriber,
)

REMOTE_BASE_URL = "http://remote.test/v1"


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


# -- WhisperCppTranscriber ---------------------------------------------------


class _FakeWhisperCppSegment:
    def __init__(self, t0: int, t1: int, text: str) -> None:
        self.t0 = t0  # centiseconds
        self.t1 = t1
        self.text = text


class _FakeWhisperCppModel:
    """Records the thread `transcribe()` runs on, so the test can prove it
    happens off the event-loop thread. Unlike faster-whisper's fake, this
    returns an already-materialized list (matching pywhispercpp's actual
    non-generator `transcribe()`)."""

    def __init__(self, record_thread: list[threading.Thread]) -> None:
        self._record_thread = record_thread

    def transcribe(self, wav_path: str) -> list[_FakeWhisperCppSegment]:
        self._record_thread.append(threading.current_thread())
        return [
            _FakeWhisperCppSegment(0, 150, "hello"),
            _FakeWhisperCppSegment(150, 300, "world"),
        ]


async def test_whisper_cpp_transcribe_materializes_segments_off_event_loop_thread() -> None:
    iterated_on: list[threading.Thread] = []
    transcriber = object.__new__(WhisperCppTranscriber)
    transcriber._model = _FakeWhisperCppModel(iterated_on)  # type: ignore[attr-defined]
    transcriber.options = {}  # type: ignore[attr-defined]

    event_loop_thread = threading.current_thread()
    result = await transcriber.transcribe(Path("unused.wav"))

    assert len(iterated_on) == 1
    assert iterated_on[0] is not event_loop_thread
    assert [s.text for s in result.segments] == ["hello", "world"]
    # centiseconds -> seconds
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 1.5), (1.5, 3.0)]
    assert result.language == ""


def test_whisper_cpp_transcriber_raises_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pywhispercpp":
            raise ImportError("no module named pywhispercpp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="pywhispercpp is not installed"):
        WhisperCppTranscriber(model_name="base")


# -- RemoteTranscriber --------------------------------------------------------


@respx.mock
async def test_remote_transcriber_verbose_json_happy_path(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake wav")
    route = respx.post(f"{REMOTE_BASE_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(
            200,
            json={
                "language": "en",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "hello"},
                    {"start": 1.0, "end": 2.0, "text": "world"},
                ],
            },
        )
    )

    transcriber = RemoteTranscriber(base_url=REMOTE_BASE_URL, model="whisper-1")
    result = await transcriber.transcribe(wav)

    assert route.call_count == 1
    assert result.language == "en"
    assert [s.text for s in result.segments] == ["hello", "world"]
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 1.0), (1.0, 2.0)]


@respx.mock
async def test_remote_transcriber_plain_json_fallback(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake wav")
    respx.post(f"{REMOTE_BASE_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hello world"})
    )

    transcriber = RemoteTranscriber(base_url=REMOTE_BASE_URL)
    result = await transcriber.transcribe(wav)

    assert len(result.segments) == 1
    assert result.segments[0].text == "hello world"
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 0.0


@respx.mock
async def test_remote_transcriber_http_error_raises_transcribe_error(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake wav")
    respx.post(f"{REMOTE_BASE_URL}/audio/transcriptions").mock(return_value=httpx.Response(500))

    transcriber = RemoteTranscriber(base_url=REMOTE_BASE_URL)
    with pytest.raises(TranscribeError):
        await transcriber.transcribe(wav)


@respx.mock
async def test_remote_transcriber_sends_auth_header_when_api_key_set(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake wav")
    route = respx.post(f"{REMOTE_BASE_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hi"})
    )

    transcriber = RemoteTranscriber(base_url=REMOTE_BASE_URL, api_key="secret-token")
    await transcriber.transcribe(wav)

    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-token"


@respx.mock
async def test_remote_transcriber_omits_auth_header_when_no_api_key(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"fake wav")
    route = respx.post(f"{REMOTE_BASE_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hi"})
    )

    transcriber = RemoteTranscriber(base_url=REMOTE_BASE_URL)
    await transcriber.transcribe(wav)

    assert "authorization" not in {h.lower() for h in route.calls[0].request.headers}
