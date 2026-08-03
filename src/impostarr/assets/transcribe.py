"""Transcription: a `Transcriber` protocol plus pluggable backend
implementations.

Transcription is a deployment choice, not a fixed dependency — the target
box's hardware (CPU only, NVIDIA GPU, non-NVIDIA iGPU, or none at all)
determines which backend makes sense, so no single implementation is
"the" transcriber. Backends are selected via `workers.transcriber` in
config and loaded through the `impostarr.transcribers` entry-point group
(`impostarr.plugins.transcribers.load_transcriber`), never hardcoded.

`faster-whisper` and `pywhispercpp` are both optional extras (`pip install
impostarr[whisper]` / `impostarr[whispercpp]`), not base dependencies —
`FasterWhisperTranscriber` and `WhisperCppTranscriber` guard their imports
at construction time so a misconfigured deployment fails loudly and early
rather than deep inside a worker task. `RemoteTranscriber` needs no local
ML library at all — it speaks an OpenAI-compatible `/audio/transcriptions`
endpoint, for offloading transcription to a server that actually has a
GPU. `NullTranscriber` is a no-op implementation for tests and pipelines
run without transcription configured (`workers.transcriber: none`).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    """A transcription backend call failed (HTTP error, bad response,
    decode failure, ...)."""


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

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        models_dir: Path | None = None,
        options: dict | None = None,
        max_concurrent: int = 1,
    ) -> None:
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
        self.options = dict(options) if options else {}
        self._model: Any = None
        # Guards lazy model construction so concurrent workers share one
        # loaded model instead of each loading their own (OOMKill risk).
        self._model_lock = asyncio.Lock()
        # Serializes heavy inference even when the pool runs more workers
        # than this — one CPU/GPU decode at a time by default.
        self._semaphore = asyncio.Semaphore(int(self.options.get("max_concurrent", max_concurrent)))

    def _resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            logger.exception("CUDA device detection failed; defaulting to cpu")
            return "cpu"

    def _load_model(self) -> Any:
        from faster_whisper import WhisperModel

        device = self._resolve_device()
        kwargs: dict[str, Any] = {
            "device": device,
            "download_root": str(self.models_dir) if self.models_dir else None,
        }
        if device == "cpu":
            kwargs["compute_type"] = "int8"
        if "compute_type" in self.options:
            kwargs["compute_type"] = self.options["compute_type"]
        return WhisperModel(self.model_name, **kwargs)

    def _transcribe_sync(self, wav_path: str) -> tuple[list[Any], str]:
        """Blocking: run the model AND fully materialize its segment
        generator, in one call. faster-whisper's `transcribe()` returns
        lazily — the actual decoding happens as the generator is iterated,
        not when `transcribe()` is called — so the iteration must happen
        inside this same `to_thread` call, not back on the event loop."""
        segments, info = self._model.transcribe(wav_path)
        return list(segments), info.language

    async def _ensure_model(self) -> None:
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load_model)

    async def transcribe(self, wav_path: Path) -> TranscriptResult:
        await self._ensure_model()
        async with self._semaphore:
            segments, language = await asyncio.to_thread(self._transcribe_sync, str(wav_path))
        segs = [TranscriptSegment(start=s.start, end=s.end, text=s.text) for s in segments]
        return TranscriptResult(segments=segs, language=language)


class WhisperCppTranscriber:
    """Lazy-loading `pywhispercpp` (whisper.cpp Python bindings) wrapper.

    CPU wheels install everywhere with no compiler (manylinux wheels on
    PyPI). A Vulkan-accelerated build (e.g. for an Intel/AMD iGPU) requires
    building `pywhispercpp` from source against a Vulkan-enabled
    whisper.cpp — not attempted here; see the README GPU section.

    Unlike faster-whisper's `transcribe()`, which returns a lazy generator
    that only actually decodes as it's iterated, pywhispercpp's
    `Model.transcribe()` is a plain blocking call that returns an already-
    materialized list of segments — so one `to_thread` call is enough to
    keep both the decode and the segment list off the event loop.
    """

    def __init__(
        self,
        model_name: str,
        models_dir: Path | None = None,
        options: dict | None = None,
        max_concurrent: int = 1,
    ) -> None:
        try:
            import pywhispercpp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "pywhispercpp is not installed; install the 'whispercpp' extra "
                "(pip install impostarr[whispercpp]) to use WhisperCppTranscriber"
            ) from exc
        self.model_name = model_name
        self.models_dir = models_dir
        self.options = dict(options) if options else {}
        self._model: Any = None
        # Guards lazy model construction so concurrent workers share one
        # loaded model instead of each loading their own (OOMKill risk).
        self._model_lock = asyncio.Lock()
        # Serializes heavy inference even when the pool runs more workers
        # than this — one CPU/GPU decode at a time by default.
        self._semaphore = asyncio.Semaphore(int(self.options.get("max_concurrent", max_concurrent)))

    def _load_model(self) -> Any:
        from pywhispercpp.model import Model

        options = {k: v for k, v in self.options.items() if k != "max_concurrent"}
        return Model(
            self.model_name,
            models_dir=str(self.models_dir) if self.models_dir else None,
            **options,
        )

    def _transcribe_sync(self, wav_path: str) -> list[Any]:
        return self._model.transcribe(wav_path)

    async def _ensure_model(self) -> None:
        if self._model is None:
            async with self._model_lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load_model)

    async def transcribe(self, wav_path: Path) -> TranscriptResult:
        await self._ensure_model()
        async with self._semaphore:
            segments = await asyncio.to_thread(self._transcribe_sync, str(wav_path))
        # whisper.cpp segment timestamps (t0/t1) are in centiseconds.
        segs = [
            TranscriptSegment(start=s.t0 / 100.0, end=s.t1 / 100.0, text=s.text)
            for s in segments
        ]
        return TranscriptResult(segments=segs, language=self.options.get("language", ""))


class RemoteTranscriber:
    """Speaks an OpenAI-compatible `/audio/transcriptions` endpoint (e.g. a
    [speaches](https://github.com/speaches-ai/speaches) or
    faster-whisper-server container) — for offloading transcription to
    whatever box actually has a usable GPU. Needs no local ML library.

    HTTP client lifecycle mirrors `SubsLlmPlugin`/`RefSubService`: an
    `httpx.AsyncClient` can be injected (caller owns it); otherwise one is
    lazily created and owned here, closable via `aclose()`.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "whisper-1",
        timeout_s: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

    async def aclose(self) -> None:
        """Closes the lazily-created owned client, if any. No-op when an
        `httpx.AsyncClient` was injected — the injector owns that
        lifecycle."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_s)
        return self._http

    async def transcribe(self, wav_path: Path) -> TranscriptResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = await self._get_http()

        try:
            with wav_path.open("rb") as f:
                resp = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers,
                    files={"file": (wav_path.name, f, "audio/wav")},
                    data={"model": self.model, "response_format": "verbose_json"},
                )
        except httpx.HTTPError as exc:
            raise TranscribeError(f"remote transcription request failed: {exc}") from exc

        if resp.status_code != 200:
            raise TranscribeError(f"remote transcription failed: HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise TranscribeError(f"remote transcription returned non-JSON response: {exc}") from exc

        if "segments" in payload:
            segs = [
                TranscriptSegment(start=s["start"], end=s["end"], text=s["text"])
                for s in payload["segments"]
            ]
        else:
            # Plain (non-verbose_json) response: {"text": "..."} only.
            segs = [TranscriptSegment(start=0.0, end=0.0, text=payload.get("text", ""))]
        return TranscriptResult(segments=segs, language=payload.get("language", ""))
