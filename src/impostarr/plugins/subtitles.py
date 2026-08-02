"""Public SRT-parsing utility for identifier plugins.

Part of impostarr's plugin-facing API (`impostarr.plugins.subtitles`) — any
plugin package, bundled or third-party, that needs to parse `.srt` reference
or embedded subtitles imports `parse_srt` from here rather than from another
plugin's package (a third-party plugin cannot import from a sibling plugin
package it doesn't depend on).
"""

from __future__ import annotations

import re


def parse_srt(text: str) -> list[str]:
    """Parse SRT text into cue line texts (index/timestamp lines discarded;
    multi-line cues joined with a space). A minimal regex/state-machine
    parser — no subtitle-parsing dependency. Tolerates a leading UTF-8 BOM,
    CRLF line endings, a missing/malformed index line (only the `-->` line
    is required to locate a cue), and skips blocks with no timestamp line
    at all (malformed cues) rather than raising."""
    text = text.lstrip("\ufeff")
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        block_lines = block.splitlines()
        ts_idx = next((i for i, line in enumerate(block_lines) if "-->" in line), None)
        if ts_idx is None:
            continue
        cue = " ".join(line.strip() for line in block_lines[ts_idx + 1 :] if line.strip())
        if cue:
            lines.append(cue)
    return lines
