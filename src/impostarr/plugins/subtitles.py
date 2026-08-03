"""Public SRT-parsing utility for identifier plugins.

Part of impostarr's plugin-facing API (`impostarr.plugins.subtitles`) — any
plugin package, bundled or third-party, that needs to parse `.srt` reference
or embedded subtitles imports `parse_srt` from here rather than from another
plugin's package (a third-party plugin cannot import from a sibling plugin
package it doesn't depend on).
"""

from __future__ import annotations

import re
from typing import Any

_TIMESTAMP_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _parse_timestamp_s(line: str) -> float | None:
    """Start time (seconds) from an SRT `-->` line's left-hand timestamp,
    or `None` if it doesn't match the expected `HH:MM:SS,mmm` shape (both
    `,` and `.` decimal separators accepted — some tools emit the latter).
    """
    match = _TIMESTAMP_RE.search(line)
    if match is None:
        return None
    hours, minutes, seconds, millis = (int(g) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _iter_cues(text: str) -> list[tuple[float | None, str]]:
    """Shared block-parsing core for `parse_srt`/`parse_srt_timed`: yields
    `(start_s, cue_text)` per cue block. A minimal regex/state-machine
    parser — no subtitle-parsing dependency. Tolerates a leading UTF-8 BOM,
    CRLF line endings, a missing/malformed index line (only the `-->` line
    is required to locate a cue), and skips blocks with no timestamp line
    at all (malformed cues) rather than raising. `start_s` is `None` when
    the timestamp line is present but doesn't parse (still an included
    cue, just not timestamp-addressable)."""
    text = text.lstrip("﻿")
    cues: list[tuple[float | None, str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block_lines = block.splitlines()
        ts_idx = next((i for i, line in enumerate(block_lines) if "-->" in line), None)
        if ts_idx is None:
            continue
        cue = " ".join(line.strip() for line in block_lines[ts_idx + 1 :] if line.strip())
        if cue:
            cues.append((_parse_timestamp_s(block_lines[ts_idx]), cue))
    return cues


def parse_srt(text: str) -> list[str]:
    """Parse SRT text into cue line texts (index/timestamp lines discarded;
    multi-line cues joined with a space). See `_iter_cues` for parsing
    details/tolerances."""
    return [cue for _, cue in _iter_cues(text)]


def parse_srt_timed(text: str) -> list[dict[str, Any]]:
    """Parse SRT text into `[{"start_s": float | None, "text": str}, ...]`
    — same cues as `parse_srt`, plus each cue's start timestamp in seconds
    (for UI features that need to address a specific line, e.g. the
    inspect panel's per-line timestamp tooltips and timeline scrubber).
    `start_s` is `None` for a cue whose timestamp line didn't parse."""
    return [{"start_s": start_s, "text": text} for start_s, text in _iter_cues(text)]
