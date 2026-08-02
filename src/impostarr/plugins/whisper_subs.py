"""`whisper-subs` identifier plugin.

Compares the job's whisper transcript against reference subtitles for
episodes near the claimed episode number, scoring how well the transcript's
content matches each candidate episode's reference SRT.

Adapter strategy decision (spec approach 2 called for importing
`mkv-episode-matcher`'s subtitle-matching internals): the library could not
be trial-installed under this project's pinned `requires-python = ">=3.12"`.
`uv add mkv-episode-matcher --frozen` fails during resolution/build because
`mkv-episode-matcher` -> `librosa` -> `numba==0.53.1` -> `llvmlite==0.36.0`,
and llvmlite 0.36.0's setup.py hard-guards `RuntimeError: Cannot install on
Python version 3.12.13; only versions >=3.6,<3.10 are supported`. Its
internals were therefore never reachable to evaluate as an importable
library, let alone as functions usable outside its CLI. Per the spec's
documented fallback, this module implements the comparison natively with
`rapidfuzz` (pinned) instead; `mkv-episode-matcher` is not a dependency.

Comparison formula (`_match_ratio`): reference SRT lines and transcript
segment texts are each lowercased and stripped of punctuation/extra
whitespace. Two signals are blended 50/50:
  - `full_ratio` — `rapidfuzz.fuzz.token_set_ratio` over the two blobs of
    joined, normalized text (0..100, scaled to 0..1). Rewards overall
    content overlap even when transcript/subtitle line segmentation differs.
  - `hit_rate` — fraction of normalized reference lines that have at least
    one transcript segment scoring >=70 token_set_ratio against them.
    Rewards a high proportion of individually corroborated lines and
    dampens a false-positive driven by one long shared phrase.
  `ratio = 0.5 * full_ratio + 0.5 * hit_rate`.

Plugin config wiring note: the loader (`plugins/loader.py`) currently
instantiates plugins with no constructor arguments (`plugin_cls()`); per
loaded-plugin config is stored on `LoadedPlugin.config` for the caller (a
pipeline stage) to apply, not injected automatically. This plugin accepts an
optional `config: WhisperSubsConfig` constructor argument, defaulting to
`WhisperSubsConfig()`, so callers/tests can supply one explicitly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rapidfuzz import fuzz

from .base import (
    AssetBundle,
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 10
_LINE_HIT_THRESHOLD = 70.0
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


class WhisperSubsConfig(BaseModel):
    min_lines: int = 20


def _normalize(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def parse_srt(text: str) -> list[str]:
    """Parse SRT text into cue line texts (index/timestamp lines discarded;
    multi-line cues joined with a space). A minimal regex/state-machine
    parser — no subtitle-parsing dependency."""
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


def _match_ratio(transcript_segments: list[dict[str, Any]], srt_lines: list[str]) -> tuple[float, int]:
    """Compare transcript segments against reference SRT lines. Returns
    (ratio in 0..1, number of compared reference lines). Formula documented
    in the module docstring."""
    transcript_lines = [_normalize(seg.get("text", "")) for seg in transcript_segments]
    transcript_lines = [line for line in transcript_lines if line]
    norm_srt = [_normalize(line) for line in srt_lines]
    norm_srt = [line for line in norm_srt if line]

    if not transcript_lines or not norm_srt:
        return 0.0, len(norm_srt)

    full_ratio = fuzz.token_set_ratio(" ".join(transcript_lines), " ".join(norm_srt)) / 100.0

    hits = sum(
        1
        for line in norm_srt
        if any(fuzz.token_set_ratio(line, seg) >= _LINE_HIT_THRESHOLD for seg in transcript_lines)
    )
    hit_rate = hits / len(norm_srt)

    return 0.5 * full_ratio + 0.5 * hit_rate, len(norm_srt)


def _series_ext_ids(series: dict[str, Any]) -> dict[str, Any]:
    ext_ids: dict[str, Any] = {}
    for key, field in (("tvdb", "tvdb_id"), ("tmdb", "tmdb_id"), ("imdb", "imdb_id")):
        value = series.get(field)
        if value is not None:
            ext_ids[key] = value
    return ext_ids


class WhisperSubsPlugin(IdentifierPlugin):
    name = "whisper-subs"
    version = "1.0.0"
    config_model = WhisperSubsConfig

    def __init__(self, config: WhisperSubsConfig | None = None) -> None:
        self.config = config or WhisperSubsConfig()

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        transcript = assets.transcript
        if not transcript:
            return PluginResult(status="abstain", reason="no transcript")

        segments: list[dict[str, Any]] = transcript.get("segments") or []
        if len(segments) < self.config.min_lines:
            return PluginResult(status="abstain", reason="transcript too short")

        season_episodes = sorted(
            (ep for ep in ctx.episodes if ep.get("season_number") == claimed.season),
            key=lambda ep: min(abs(ep["episode_number"] - c) for c in claimed.episodes),
        )[:_MAX_CANDIDATES]

        ext_ids = _series_ext_ids(ctx.series)

        candidates: list[Candidate] = []
        claimed_covered = False
        try:
            for ep in season_episodes:
                ep_num = ep["episode_number"]
                srt_path = await ctx.refsubs.get(ext_ids, claimed.season, ep_num)
                if srt_path is None:
                    continue
                srt_text = Path(srt_path).read_text(encoding="utf-8", errors="replace")
                srt_lines = parse_srt(srt_text)
                ratio, compared = _match_ratio(segments, srt_lines)
                if ep_num in claimed.episodes:
                    claimed_covered = True
                candidates.append(
                    Candidate(
                        confidence=ratio,
                        ident=CandidateIdent(
                            series="claimed", season=claimed.season, episodes=[ep_num]
                        ),
                        numbering="tvdb",
                        evidence={
                            "match_ratio": ratio,
                            "compared_lines": compared,
                            "refsub_path": str(srt_path),
                        },
                    )
                )
        except Exception as exc:
            logger.exception("whisper-subs comparison failed")
            return PluginResult(status="error", reason=f"comparison failed: {exc}")

        if not candidates:
            return PluginResult(status="abstain", reason="no reference subtitles")

        if not claimed_covered:
            candidates.append(
                Candidate(
                    confidence=0.0,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=list(claimed.episodes)
                    ),
                    numbering="tvdb",
                    evidence={"note": "no reference subs for claimed"},
                )
            )

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return PluginResult(status="ok", candidates=candidates)
