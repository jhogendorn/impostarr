"""Transcriber backend discovery/loading via the `impostarr.transcribers`
entry-point group — the transcription analogue of `plugins/loader.py`'s
identifier-plugin loading, minus the per-plugin enable/weight/options
model (a deployment runs exactly one transcriber, chosen by name, not a
list of them).

Each entry point resolves to a *factory callable* `(WorkersConfig, Path) ->
Transcriber`, not a class — so built-ins and third-party backends (e.g. an
OpenVINO transcriber) share one construction protocol regardless of their
own constructor's shape. `faster_whisper_factory`, `whisper_cpp_factory`,
`remote_factory`, and `none_factory` below are the built-ins registered in
`pyproject.toml`.

Construction failures (missing optional dependency, misconfiguration —
both surface as `RuntimeError`, see `FasterWhisperTranscriber`/
`WhisperCppTranscriber`'s import guards and `remote_factory`'s option
check) are logged and degrade to `NullTranscriber` rather than crashing
startup — mirroring `load_plugins`'s never-crash-the-loader discipline,
applied here to a single required component instead of a list. An unknown
`workers.transcriber` name degrades the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

from ..assets.transcribe import (
    FasterWhisperTranscriber,
    NullTranscriber,
    RemoteTranscriber,
    Transcriber,
    WhisperCppTranscriber,
)
from ..config import Settings, WorkersConfig

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "impostarr.transcribers"

TranscriberFactory = Callable[[WorkersConfig, Path], Transcriber]


def faster_whisper_factory(workers: WorkersConfig, models_dir: Path) -> Transcriber:
    return FasterWhisperTranscriber(
        model_name=workers.whisper_model, device=workers.whisper_device, models_dir=models_dir
    )


def whisper_cpp_factory(workers: WorkersConfig, models_dir: Path) -> Transcriber:
    return WhisperCppTranscriber(
        model_name=workers.whisper_model, models_dir=models_dir, options=workers.transcriber_options
    )


def remote_factory(workers: WorkersConfig, models_dir: Path) -> Transcriber:
    options = workers.transcriber_options
    base_url = options.get("base_url")
    if not base_url:
        raise RuntimeError("the 'remote' transcriber requires workers.transcriber_options.base_url")
    return RemoteTranscriber(
        base_url=base_url,
        api_key=options.get("api_key", ""),
        model=options.get("model", "whisper-1"),
        timeout_s=float(options.get("timeout_s", 120.0)),
    )


def none_factory(workers: WorkersConfig, models_dir: Path) -> Transcriber:
    return NullTranscriber()


def load_transcriber(settings: Settings) -> Transcriber:
    name = settings.workers.transcriber
    eps = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}

    ep = eps.get(name)
    if ep is None:
        logger.error(
            "configured transcriber %r has no matching entry point; falling back to NullTranscriber",
            name,
        )
        return NullTranscriber()

    try:
        factory = ep.load()
        return factory(settings.workers, settings.models_dir)
    except Exception:
        logger.exception(
            "failed to construct transcriber %r; falling back to NullTranscriber", name
        )
        return NullTranscriber()
