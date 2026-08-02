"""Transcription: a `Transcriber` protocol plus two implementations.

`faster-whisper` is an optional extra (`pip install impostarr[whisper]`),
not a base dependency — `FasterWhisperTranscriber` guards the import at
construction time so a misconfigured deployment fails loudly and early
rather than deep inside a worker task. `NullTranscriber` is a no-op
implementation for tests and pipelines run without ML configured.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str


class TranscriptResult(BaseModel):
    segments: list[TranscriptSegment]
    language: str


class Transcriber(Protocol):
    async def transcribe(self, wav_path: Path) -> TranscriptResult: ...


class NullTranscriber:
    """Always returns an empty transcript; never touches the filesystem."""

    async def transcribe(self, wav_path: Path) -> TranscriptResult:
        return TranscriptResult(segments=[], language="")


class FasterWhisperTranscriber:
    """Lazy-loading `faster-whisper` wrapper.

    The `faster-whisper` import is checked at construction (raises
    `RuntimeError` immediately if the optional extra isn't installed); the
    model itself is loaded lazily on the first `transcribe` call, so
    construction never downloads or loads a model.
    """

    def __init__(self, model_name: str, device: str = "cpu", models_dir: Path | None = None) -> None:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed; install the 'whisper' extra "
                "(pip install impostarr[whisper]) to use FasterWhisperTranscriber"
            ) from exc
        self.model_name = model_name
        self.device = device
        self.models_dir = models_dir
        self._model: Any = None

    def _load_model(self) -> Any:
        from faster_whisper import WhisperModel

        return WhisperModel(
            self.model_name,
            device=self.device,
            download_root=str(self.models_dir) if self.models_dir else None,
        )

    def _transcribe_sync(self, wav_path: str) -> tuple[list[Any], str]:
        """Blocking: run the model AND fully materialize its segment
        generator, in one call. faster-whisper's `transcribe()` returns
        lazily — the actual decoding happens as the generator is iterated,
        not when `transcribe()` is called — so the iteration must happen
        inside this same `to_thread` call, not back on the event loop."""
        segments, info = self._model.transcribe(wav_path)
        return list(segments), info.language

    async def transcribe(self, wav_path: Path) -> TranscriptResult:
        if self._model is None:
            self._model = await asyncio.to_thread(self._load_model)
        segments, language = await asyncio.to_thread(self._transcribe_sync, str(wav_path))
        segs = [TranscriptSegment(start=s.start, end=s.end, text=s.text) for s in segments]
        return TranscriptResult(segments=segs, language=language)
