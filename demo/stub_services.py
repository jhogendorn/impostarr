"""Stub OpenAI-compatible services for the demo: `/v1/audio/transcriptions`
(consumed by the `remote` transcriber backend, for whisper-subs) and
`/v1/chat/completions` (consumed by the `subs-llm` plugin). One container,
two routers — both are cheap enough to not warrant separate images.

Real speech-to-text/LLM inference is unnecessary (and slow) for a
synthetic-content demo: both endpoints answer purely by pattern-matching
data `generate_media.py` already generated, rather than doing any real
transcription or inference.

## /v1/audio/transcriptions

Fingerprints the uploaded wav purely by its duration and looks up canned
dialogue from a manifest built by `generate_media.py`, chunked into one
transcript segment per manifest line.

Duration-as-fingerprint matches the caller's actual behavior:
`impostarr.assets.extract.extract_audio` slices 16kHz mono 16-bit audio
starting at offset 60s (for any source longer than that), so the wav this
endpoint receives is `min(900, total - 60)` seconds long — the manifest is
keyed on that slice length, not the source file's own duration (see
`generate_media.py`'s module docstring).

## /v1/chat/completions

`subs-llm` sends a prompt containing an excerpt of the file's embedded
subtitles (see `impostarr_plugin_subs_llm.plugin._build_prompt`). Every
content set's dialogue contains one deterministic line — "Log entry
episode <N>" (see `generate_media.py`'s `dialogue_lines`) — naming its own
canonical episode number; this endpoint regexes that line out of the
prompt and answers with it, in the OpenAI chat-completions response shape
`subs-llm` expects: `{"season": 1, "episodes": [N], "confidence": 0.95,
"reasoning": "stub"}`. The demo only ever sends prompts built from its own
generated dialogue, so the marker is always present — the "no marker
found" branch below is a defensive fallback, not a tested path.
"""

from __future__ import annotations

import json
import os
import re
import wave
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile

MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", "/manifest/manifest.json"))
TOLERANCE_S = 2.0

_EPISODE_CUE_RE = re.compile(r"log entry episode (\d+)", re.IGNORECASE)

app = FastAPI()


def _wav_duration_s(fileobj) -> float:
    with wave.open(fileobj, "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
    return frames / rate if rate else 0.0


def _load_manifest() -> dict[str, list[str]]:
    return json.loads(MANIFEST_PATH.read_text())


def _closest_key(manifest: dict[str, list[str]], duration_s: float) -> str | None:
    best_key: str | None = None
    best_diff: float | None = None
    for key in manifest:
        diff = abs(float(key) - duration_s)
        if best_diff is None or diff < best_diff:
            best_key, best_diff = key, diff
    if best_key is not None and best_diff is not None and best_diff <= TOLERANCE_S:
        return best_key
    return None


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),  # noqa: B008 (standard FastAPI pattern)
    model: str = Form("stub"),
    response_format: str = Form("verbose_json"),
) -> dict:
    duration_s = _wav_duration_s(file.file)
    manifest = _load_manifest()
    key = _closest_key(manifest, duration_s)
    if key is None:
        return {"segments": [], "language": "en"}

    lines = manifest[key]
    seg_len = duration_s / len(lines) if lines else 0.0
    segments = [
        {"start": round(i * seg_len, 3), "end": round((i + 1) * seg_len, 3), "text": text}
        for i, text in enumerate(lines)
    ]
    return {"segments": segments, "language": "en"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict:
    body = await request.json()
    messages = body.get("messages", [])
    prompt_text = "\n".join(
        m.get("content", "") for m in messages if isinstance(m.get("content"), str)
    )
    match = _EPISODE_CUE_RE.search(prompt_text)
    if match is not None:
        episode = int(match.group(1))
        payload = {
            "season": 1,
            "episodes": [episode],
            "confidence": 0.95,
            "reasoning": "stub",
        }
    else:
        # Defensive fallback only — the demo always embeds the marker.
        payload = {
            "season": 1,
            "episodes": [],
            "confidence": 0.2,
            "reasoning": "stub: no episode cue found in prompt",
        }
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}
