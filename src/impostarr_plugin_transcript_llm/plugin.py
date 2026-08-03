"""`transcript-llm` identifier plugin.

Sibling of `impostarr_plugin_subs_llm`: same LLM episode-identification
contract (`{season, episodes, confidence, reasoning}` JSON), same provider-
failover client (`impostarr.llm.LlmClient`), same claimed/derived
candidate-building logic (`impostarr.llm.build_episode_candidates`) — but
the evidence fed to the model is the job's *whisper transcript*
(`AssetBundle.transcript`) instead of embedded subtitles, so it also works
for files with no burned-in/embedded subs, as long as whisper transcription
ran.

ASR-tuned prompt: a whisper transcript is machine-generated and noisier
than an authored subtitle track (misheard words, run-on segments, no
speaker labels) — the prompt says so explicitly ("this is an automatic
speech recognition transcript; expect errors") so the model doesn't over-
trust exact wording the way it reasonably would for real subtitles.

Segment cap: only the first `max_segments` transcript segments (default
100) are sent, mirroring subs-llm's `max_cues` — keeps the prompt bounded
for long transcripts without materially hurting identification (episode-
identifying content tends to appear early: cold opens, title cards,
recaps).

Abstains when there's no transcript, or fewer than `min_segments` (default
20) segments — mirrors whisper-subs' own `min_lines` guard, since a very
short transcript is weak identification evidence regardless of technique.

This package is a standalone, installable plugin — it depends on
`impostarr` (for the `IdentifierPlugin` contract and the shared `llm`
module) but is not part of the `impostarr` package itself, and does not
import from `impostarr_plugin_subs_llm` (or any other plugin package):
shared logic lives in `impostarr.llm`, not by reaching across plugins.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from impostarr.llm import (
    LlmClient,
    LlmContentError,
    LlmProvider,
    LlmUnavailable,
    build_episode_candidates,
    episode_json_valid,
)
from impostarr.plugins.base import (
    AssetBundle,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_MAX_ATTEMPTS = 2


class _LlmError(RuntimeError):
    """Raised for any LLM-call failure (provider-unavailable fallthrough
    exhausted, or a content failure); caught once in `identify` and mapped
    to a PluginResult(status="error")."""


class TranscriptLlmConfig(BaseModel):
    base_url: str = DEFAULT_BASE_URL
    model: str = "gpt-4o-mini"
    api_key: str = ""
    max_segments: int = Field(default=100, ge=1)
    min_segments: int = Field(default=20, ge=1)
    timeout_s: float = Field(default=60, gt=0)
    # Ordered provider failover list; takes precedence over base_url/model/
    # api_key above when set (those remain the single-provider degenerate
    # case for config back-compat).
    providers: list[LlmProvider] | None = None

    def resolved_providers(self) -> list[LlmProvider]:
        if self.providers:
            return self.providers
        return [LlmProvider(name="default", base_url=self.base_url, model=self.model, api_key=self.api_key)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build_prompt(ctx: SeriesContext, claimed: ClaimedIdent, segments: list[dict[str, Any]]) -> str:
    series_title = ctx.series.get("title", "the series")
    season_episodes = sorted(
        (ep for ep in ctx.episodes if ep.get("season_number") == claimed.season),
        key=lambda ep: ep["episode_number"],
    )
    has_titles = any(ep.get("title") for ep in season_episodes)

    if has_titles:
        lines = []
        for ep in season_episodes:
            line = f"- Episode {ep['episode_number']}: {ep.get('title')}"
            overview = ep.get("overview")
            if overview:
                line += f" — {overview}"
            lines.append(line)
        episode_list = "\n".join(lines)
        titles_note = ""
    else:
        episode_list = "\n".join(f"- Episode {ep['episode_number']}" for ep in season_episodes)
        titles_note = "\n(Episode titles are unavailable; only episode numbers are listed.)"

    transcript_text = "\n".join(seg.get("text", "").strip() for seg in segments)

    return (
        f'You are identifying which episode of the TV series "{series_title}" a video file '
        "contains, based on a whisper transcript of its audio. This is an automatic speech "
        "recognition transcript; expect errors -- misheard words, run-on or truncated "
        "sentences, and no speaker labels. Weigh the overall content and events described "
        "over exact wording.\n\n"
        f"Season {claimed.season} episodes:{titles_note}\n{episode_list}\n\n"
        f"Transcript excerpt ({len(segments)} segments):\n{transcript_text}\n\n"
        "Which episode (season, episode number(s)) is this? Reply with JSON only, in exactly "
        'this form: {"season": <int>, "episodes": [<int>, ...], "confidence": <0..1>, '
        '"reasoning": <string>}.'
    )


class TranscriptLlmPlugin(IdentifierPlugin):
    name = "transcript-llm"
    version = "1.0.0"
    config_model = TranscriptLlmConfig

    def __init__(
        self, config: TranscriptLlmConfig | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        config = config or TranscriptLlmConfig()
        super().__init__(config)
        self._llm = LlmClient(config.resolved_providers(), http=http, timeout_s=config.timeout_s)

    async def aclose(self) -> None:
        """Closes the lazily-created owned client, if any. No-op when an
        `httpx.AsyncClient` was injected — the injector owns that
        lifecycle."""
        await self._llm.aclose()

    async def _call_llm(self, prompt: str) -> tuple[dict[str, Any], str]:
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        try:
            return await self._llm.chat_json(
                messages, validate=episode_json_valid, max_attempts=_MAX_ATTEMPTS
            )
        except (LlmUnavailable, LlmContentError) as exc:
            raise _LlmError(str(exc)) from exc

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        transcript = assets.transcript
        if not transcript:
            return PluginResult(status="abstain", reason="no transcript")

        segments: list[dict[str, Any]] = transcript.get("segments") or []
        if len(segments) < self.config.min_segments:
            return PluginResult(status="abstain", reason="transcript too short")

        if not self.config.providers and not self.config.api_key and self.config.base_url == DEFAULT_BASE_URL:
            return PluginResult(status="abstain", reason="no LLM configured")

        segments = segments[: self.config.max_segments]
        prompt = _build_prompt(ctx, claimed, segments)

        try:
            data, provider_name = await self._call_llm(prompt)
            llm_season = int(data["season"])
            llm_episodes = [int(e) for e in data["episodes"]]
            llm_confidence = _clamp01(float(data.get("confidence", 0.0)))
            reasoning = str(data.get("reasoning", ""))
            provider_model = next(
                (p.model for p in self._llm.providers if p.name == provider_name), self.config.model
            )

            candidates = build_episode_candidates(
                llm_season,
                llm_episodes,
                llm_confidence,
                claimed.season,
                claimed.episodes,
                {
                    "reasoning": reasoning,
                    "segment_count": len(segments),
                    "model": provider_model,
                    "provider": provider_name,
                },
            )
        except _LlmError as exc:
            logger.warning("transcript-llm call failed: %s", exc)
            return PluginResult(status="error", reason=str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            logger.exception("transcript-llm returned an unusable response")
            return PluginResult(status="error", reason=f"malformed LLM response: {exc}")
        except Exception as exc:
            # Belt-and-braces, matching subs-llm/whisper-subs: an identifier
            # plugin must never raise out of identify().
            logger.exception("transcript-llm identify failed")
            return PluginResult(status="error", reason=f"transcript-llm failed: {exc}")

        return PluginResult(status="ok", candidates=candidates)
