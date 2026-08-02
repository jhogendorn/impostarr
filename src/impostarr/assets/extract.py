"""ffmpeg/ffprobe extraction helpers: probe, audio slice, embedded subs,
frame sampling + perceptual hashing.

Filesystem + subprocess only — DB persistence of the returned
`ExtractedAsset` records (into the `assets`/`frame_hashes` tables) is the
worker pipeline's job, not this module's. Each helper takes an `out_dir` it
writes into; the caller owns directory layout beyond that. Output filenames
embed a prefix of the record's fingerprint so re-running the same operation
with the same params on an unchanged file overwrites the same path rather
than accumulating duplicates.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import imagehash
import xxhash
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# ffprobe codec_name -> extraction handling.
TEXT_SUB_CODECS = {"subrip", "ass", "mov_text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle": "sup", "dvd_subtitle": "sub"}

PHASH_ALGO = "phash"
PHASH_VERSION = 1
PHASH_BITS = 64  # imagehash.phash default hash_size=8 -> 8x8 = 64 bits
THUMBNAIL_MAX_WIDTH = 480


class ExtractError(RuntimeError):
    """An ffmpeg/ffprobe subprocess exited non-zero; message includes stderr."""


class ExtractedAsset(BaseModel):
    """A filesystem extraction result, shaped to mirror `models.Asset` minus
    the DB-only fields (id, file_id, created_at)."""

    model_config = ConfigDict(extra="ignore")

    type: str
    path: str | None = None
    payload: dict[str, Any] | None = None
    fingerprint: str
    tool_meta: dict[str, Any] = Field(default_factory=dict)


class FrameHashSeq(BaseModel):
    """A perceptual-hash sequence, shaped to mirror `models.FrameHash` minus
    the DB-only fields."""

    model_config = ConfigDict(extra="ignore")

    algo: str = PHASH_ALGO
    version: int = PHASH_VERSION
    timestamps: list[float]
    hashes: list[str]


async def _run(cmd: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ExtractError(
            f"{cmd[0]} failed (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout


async def _tool_version(tool: str) -> str:
    out = await _run([tool, "-version"])
    first_line = out.decode(errors="replace").splitlines()[0]
    # e.g. "ffmpeg version 8.1.2 Copyright (c) 2000-2026 ..."
    parts = first_line.split()
    return parts[2] if len(parts) > 2 else first_line


def fingerprint(path: Path, operation: str, params: str) -> str:
    """xxh64 of `{path}:{size}:{mtime}:{operation}:{params}` — deterministic
    across re-runs on an unchanged source file, changes if the file or the
    operation's params change."""
    st = path.stat()
    key = f"{path}:{st.st_size}:{st.st_mtime}:{operation}:{params}"
    return xxhash.xxh64(key.encode()).hexdigest()


def _asset_name(path: Path, kind: str, fp: str, ext: str) -> str:
    return f"{path.stem}-{kind}-{fp[:16]}.{ext}"


def _duration_s(probe_payload: dict[str, Any]) -> float:
    return float(probe_payload["format"]["duration"])


async def probe(path: Path) -> ExtractedAsset:
    """ffprobe JSON (streams, format/duration, container) as the asset payload."""
    out = await _run(
        [
            FFPROBE,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    payload = json.loads(out)
    version = await _tool_version(FFPROBE)
    fp = fingerprint(path, "probe", "")
    return ExtractedAsset(
        type="probe",
        payload=payload,
        fingerprint=fp,
        tool_meta={"ffprobe_version": version},
    )


async def extract_audio(
    path: Path,
    out_dir: Path,
    offset_s: float = 60.0,
    duration_s: float = 900.0,
) -> ExtractedAsset:
    """16kHz mono wav slice. Starts at `offset_s` unless the file is shorter
    than that (then starts at 0); takes `min(duration_s, remaining)`."""
    probe_asset = await probe(path)
    total = _duration_s(probe_asset.payload or {})
    start = 0.0 if total <= offset_s else offset_s
    take = min(duration_s, total - start)

    params = f"offset={offset_s}:duration={duration_s}"
    fp = fingerprint(path, "extract_audio", params)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _asset_name(path, "audio", fp, "wav")

    await _run(
        [
            FFMPEG,
            "-y",
            "-ss",
            str(start),
            "-t",
            str(take),
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out_path),
        ]
    )
    version = await _tool_version(FFMPEG)
    return ExtractedAsset(
        type="audio",
        path=str(out_path),
        fingerprint=fp,
        tool_meta={"ffmpeg_version": version, "start_s": start, "duration_s": take},
    )


async def extract_embedded_subs(path: Path, out_dir: Path) -> list[ExtractedAsset]:
    """Text-sub streams (subrip/ass/mov_text) extracted to .srt; image-sub
    streams (hdmv_pgs_subtitle/dvd_subtitle) raw-copied for the OCR plugin.
    One asset per subtitle stream, in declared stream order."""
    probe_asset = await probe(path)
    streams = (probe_asset.payload or {}).get("streams", [])
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    out_dir.mkdir(parents=True, exist_ok=True)
    version = await _tool_version(FFMPEG)
    assets: list[ExtractedAsset] = []

    for sub_idx, stream in enumerate(sub_streams):
        codec = stream.get("codec_name")
        language = (stream.get("tags") or {}).get("language")
        params = f"stream={sub_idx}:codec={codec}"
        fp = fingerprint(path, "extract_embedded_subs", params)
        tool_meta: dict[str, Any] = {
            "ffmpeg_version": version,
            "stream_index": sub_idx,
            "codec_name": codec,
        }
        if language:
            tool_meta["language"] = language

        if codec in TEXT_SUB_CODECS:
            out_path = out_dir / _asset_name(path, "subs", fp, "srt")
            await _run(
                [
                    FFMPEG,
                    "-y",
                    "-i",
                    str(path),
                    "-map",
                    f"0:s:{sub_idx}",
                    "-c:s",
                    "srt",
                    str(out_path),
                ]
            )
        elif codec in IMAGE_SUB_CODECS:
            ext = IMAGE_SUB_CODECS[codec]
            out_path = out_dir / _asset_name(path, "subs", fp, ext)
            await _run(
                [
                    FFMPEG,
                    "-y",
                    "-i",
                    str(path),
                    "-map",
                    f"0:s:{sub_idx}",
                    "-c:s",
                    "copy",
                    str(out_path),
                ]
            )
        else:
            # Unsupported subtitle codec (e.g. image subs we don't handle
            # yet); skip rather than fail the whole extraction.
            continue

        assets.append(
            ExtractedAsset(type="subs", path=str(out_path), fingerprint=fp, tool_meta=tool_meta)
        )

    return assets


async def sample_frames(
    path: Path, out_dir: Path, n: int = 16
) -> tuple[FrameHashSeq, list[ExtractedAsset]]:
    """`n` frames at deterministic timestamps `(i+0.5)/n * duration`, each
    hashed with imagehash.phash and saved as a <=480px-wide jpeg thumbnail."""
    probe_asset = await probe(path)
    total = _duration_s(probe_asset.payload or {})
    timestamps = [(i + 0.5) / n * total for i in range(n)]

    out_dir.mkdir(parents=True, exist_ok=True)
    version = await _tool_version(FFMPEG)
    assets: list[ExtractedAsset] = []
    hashes: list[str] = []

    for i, ts in enumerate(timestamps):
        params = f"n={n}:index={i}:ts={ts:.6f}"
        fp = fingerprint(path, "sample_frames", params)
        out_path = out_dir / _asset_name(path, "frame", fp, "jpg")

        await _run(
            [
                FFMPEG,
                "-y",
                "-ss",
                f"{ts:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"scale=w={THUMBNAIL_MAX_WIDTH}:h=-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(out_path),
            ]
        )
        with Image.open(out_path) as img:
            phash = imagehash.phash(img)
        hashes.append(str(phash))

        assets.append(
            ExtractedAsset(
                type="frames",
                path=str(out_path),
                fingerprint=fp,
                tool_meta={"ffmpeg_version": version, "index": i, "timestamp": ts},
            )
        )

    seq = FrameHashSeq(timestamps=timestamps, hashes=hashes)
    return seq, assets


def hamming_similarity(seq_a: FrameHashSeq, seq_b: FrameHashSeq) -> float:
    """Mean per-frame similarity (1 - hamming_distance/64), aligned by index,
    over min(len(a), len(b)) frames. 0.0 if either sequence is empty."""
    n = min(len(seq_a.hashes), len(seq_b.hashes))
    if n == 0:
        return 0.0
    total = 0.0
    for i in range(n):
        hash_a = imagehash.hex_to_hash(seq_a.hashes[i])
        hash_b = imagehash.hex_to_hash(seq_b.hashes[i])
        distance = hash_a - hash_b
        total += 1 - distance / PHASH_BITS
    return total / n
