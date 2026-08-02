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
segment texts are each stripped of subtitle markup (HTML-style tags like
`<i>...</i>`, ASS override blocks like `{\an8}`), lowercased, and stripped
of punctuation/extra whitespace — markup removal runs first so tag
delimiters (`<`, `>`, `{`, `}`) never fuse with adjacent words once
punctuation is stripped (e.g. `<i>Hello</i>` must not become "ihelloi").
Two signals are blended 50/50:
  - `full_ratio` — `rapidfuzz.fuzz.token_set_ratio` over the two blobs of
    joined, normalized text (0..100, scaled to 0..1). Rewards overall
    content overlap even when transcript/subtitle line segmentation differs.
  - `hit_rate` — fraction of normalized reference lines that have at least
    one transcript segment scoring >=70 token_set_ratio against them.
    Rewards a high proportion of individually corroborated lines and
    dampens a false-positive driven by one long shared phrase.
  `ratio = 0.5 * full_ratio + 0.5 * hit_rate`.

Thin-reference discount: a reference SRT with very few compared lines can
spuriously score high (e.g. a 2-line SRT that happens to match well isn't
strong evidence). `WhisperSubsConfig.min_compared` (default 10) sets the
line count below which confidence is discounted proportionally:
`discount = min(1.0, compared_lines / min_compared)`,
`confidence = ratio * discount`. The discount factor is recorded in
evidence as `thin_refsub_discount`.

Reference subtitle fetches for the candidate window are issued concurrently
via `asyncio.gather` — `RefSubService` is designed for concurrent use (its
quota reservation guards concurrent `get()` calls with an internal lock).
Results are zipped back against the window list in its original,
distance-sorted order, so candidate ordering stays deterministic regardless
of fetch completion order.

Plugin config wiring: the loader (`impostarr.plugins.loader`) validates
`Settings.plugins.identifiers["whisper-subs"].options` into `WhisperSubsConfig`
and passes it to the constructor as `config`, per the `IdentifierPlugin` base
class's `__init__(self, config=None)` convention (stored on `self.config`).
This plugin overrides `__init__` only to default `self.config` to
`WhisperSubsConfig()` when no config is supplied (e.g. direct instantiation
in tests), rather than leaving it `None`.

This package is a standalone, installable plugin — it depends on
`impostarr` (for the `IdentifierPlugin` contract and `parse_srt`) but is not
part of the `impostarr` package itself, exemplifying how a third-party
identifier plugin is structured: own package, own `pyproject.toml` entry
point, own config model, importing only from `impostarr.plugins.*`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from impostarr.plugins.base import (
    AssetBundle,
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)
from impostarr.plugins.subtitles import parse_srt

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 10
_LINE_HIT_THRESHOLD = 70.0
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


class WhisperSubsConfig(BaseModel):
    min_lines: int = Field(default=20, ge=1)
    min_compared: int = Field(default=10, ge=1)


def _normalize(text: str) -> str:
    # Markup stripped first: removing punctuation before markup would fuse
    # tag delimiters into adjacent words (e.g. "<i>Hello</i>" -> "ihelloi").
    text = _HTML_TAG_RE.sub("", text)
    text = _ASS_OVERRIDE_RE.sub("", text)
    text = text.lower()
    text = _PUNCT_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


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
        super().__init__(config or WhisperSubsConfig())

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
            # Fetched concurrently — RefSubService's quota reservation is
            # lock-guarded for exactly this. gather() preserves per-input
            # result order, so zipping back against season_episodes (already
            # distance-sorted) keeps candidate ordering deterministic.
            srt_paths = await asyncio.gather(
                *(
                    ctx.refsubs.get(ext_ids, claimed.season, ep["episode_number"])
                    for ep in season_episodes
                )
            )
            for ep, srt_path in zip(season_episodes, srt_paths, strict=True):
                if srt_path is None:
                    continue
                ep_num = ep["episode_number"]
                srt_text = Path(srt_path).read_text(encoding="utf-8", errors="replace")
                srt_lines = parse_srt(srt_text)
                ratio, compared = _match_ratio(segments, srt_lines)
                discount = min(1.0, compared / self.config.min_compared)
                discounted_ratio = ratio * discount
                if ep_num in claimed.episodes:
                    claimed_covered = True
                candidates.append(
                    Candidate(
                        confidence=discounted_ratio,
                        ident=CandidateIdent(
                            series="claimed", season=claimed.season, episodes=[ep_num]
                        ),
                        numbering="tvdb",
                        evidence={
                            "match_ratio": ratio,
                            "compared_lines": compared,
                            "refsub_path": str(srt_path),
                            "thin_refsub_discount": discount,
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
