"""`subs-llm` identifier plugin.

Adapter strategy decision (spec approach 2: adapt tvidentify
https://github.com/ram-nat/tvidentify): a time-boxed `uv add` trial installed
tvidentify cleanly under this project's `requires-python = ">=3.12"` pin
(unlike Task 11's mkv-episode-matcher, which was uninstallable). Its
internals *were* importable — `tvidentify.episode_identifier.identify_episode`
exists as a plain function. Inspection of that function and its
`_identify_episode_openai` helper showed it does not meet this task's
requirements, though:
  - The OpenAI call is hardwired to the `openai` SDK's `OpenAI()` client with
    no `base_url` parameter exposed, so an OpenAI-compatible endpoint (e.g.
    Ollama) cannot be targeted — only the `openai`/`google`/`perplexity`
    providers `identify_episode` hardcodes are reachable.
  - The API key is read only from the `OPENAI_API_KEY`/`GOOGLE_API_KEY`/
    `PERPLEXITY_API_KEY` env vars, not passed as a parameter, so it cannot be
    sourced from this project's per-plugin config.
  - The response schema is `{"season": ..., "episode": ...}` (singular,
    0-100 confidence, no multi-episode support), not this task's
    `{season, episodes, confidence 0..1, reasoning}` contract.
  - Malformed JSON is recovered via a best-effort markdown-fence regex with
    no retry, not the "one retry with a reminder message" behavior required
    here.
  - Its OCR path (`subtitle_handlers.py`) additionally pulls in
    `opencv-python-headless` (46MB) and `pytesseract` for PGS/VobSub frame
    OCR — more than this PoC needs, since image subs are out of scope here
    (see below).

None of the above is reachable without forking the library, which is out of
scope for a PoC adapter. Per the spec's documented fallback, this module
talks to the OpenAI-compatible chat-completions endpoint directly via
`httpx` instead; `tvidentify` is not a dependency, and no `openai` SDK
dependency is introduced either.

Image-sub OCR (PGS `.sup` / VobSub `.sub`) is NOT supported in this PoC:
only the first `.srt` path in `AssetBundle.sub_paths` is used; a bundle with
sub_paths but no `.srt` among them abstains with reason "PGS/VobSub OCR not
supported in PoC". This is a documented limitation, not an oversight.

SRT parsing uses `impostarr.plugins.subtitles.parse_srt` (the same minimal
cue parser shared by the `whisper-subs` bundled plugin; a third-party plugin
package cannot import from a sibling plugin package, so shared parsing logic
lives in `impostarr`'s plugin-facing API instead).

HTTP client lifecycle: mirrors `RefSubService`'s injection pattern — an
`httpx.AsyncClient` can be passed to the constructor (caller owns it, e.g.
Task 14's pipeline sharing one client across plugins); otherwise it's
lazily created and owned by the underlying `LlmClient`, closable via this
plugin's `aclose()`.

Provider failover: the actual HTTP/retry machinery lives in
`impostarr.llm.LlmClient` (shared with `impostarr_plugin_transcript_llm`),
imported here rather than reimplemented — an intentional, documented
exception to "importing only from `impostarr.plugins.*`" above: `llm.py`
is shared plugin-facing infrastructure, same category as
`impostarr.plugins.subtitles.parse_srt`, just not nested under the
`plugins` package. `base_url`/`model`/`api_key` remain the single-provider
degenerate case for config back-compat; an explicit `providers` list takes
precedence when set (`SubsLlmConfig.resolved_providers()`). Malformed-JSON
retry (reminder message, appending the model's own invalid reply first so
small models can see what they just said) is `LlmClient`'s job now; this
module only supplies the episode-JSON shape validator
(`episode_json_valid`) so a syntactically-valid-but-wrong-shape reply (e.g.
`episodes` not a list) still triggers the retry instead of failing outright.

This package is a standalone, installable plugin — it depends on
`impostarr` (for the `IdentifierPlugin` contract, `parse_srt`, and the
shared `llm` module) but is not part of the `impostarr` package itself,
exemplifying how a third-party identifier plugin is structured: own
package, own `pyproject.toml` entry point, own config model.
"""

from __future__ import annotations

import logging
from pathlib import Path
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
from impostarr.plugins.subtitles import parse_srt

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_MAX_ATTEMPTS = 2


class _LlmError(RuntimeError):
    """Raised for any LLM-call failure (provider-unavailable fallthrough
    exhausted, or a content failure); caught once in `identify` and mapped
    to a PluginResult(status="error")."""


class SubsLlmConfig(BaseModel):
    base_url: str = DEFAULT_BASE_URL
    model: str = "gpt-4o-mini"
    api_key: str = ""
    max_cues: int = Field(default=80, ge=1)
    timeout_s: float = Field(default=60, gt=0)
    # Ordered provider failover list; takes precedence over base_url/model/
    # api_key above when set (those remain the single-provider degenerate
    # case for config back-compat).
    providers: list[LlmProvider] | None = None

    def resolved_providers(self) -> list[LlmProvider]:
        if self.providers:
            return self.providers
        return [LlmProvider(name="default", base_url=self.base_url, model=self.model, api_key=self.api_key)]


def _first_srt(sub_paths: list[str]) -> str | None:
    for p in sub_paths:
        if p.lower().endswith(".srt"):
            return p
    return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build_prompt(ctx: SeriesContext, claimed: ClaimedIdent, cues: list[str]) -> str:
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

    cue_text = "\n".join(cues)

    return (
        f'You are identifying which episode of the TV series "{series_title}" a video file '
        "contains, based on its subtitles.\n\n"
        f"Season {claimed.season} episodes:{titles_note}\n{episode_list}\n\n"
        f"Subtitle excerpt ({len(cues)} cues):\n{cue_text}\n\n"
        "Which episode (season, episode number(s)) is this? Reply with JSON only, in exactly "
        'this form: {"season": <int>, "episodes": [<int>, ...], "confidence": <0..1>, '
        '"reasoning": <string>}.'
    )


class SubsLlmPlugin(IdentifierPlugin):
    name = "subs-llm"
    version = "1.0.0"
    config_model = SubsLlmConfig

    def __init__(
        self, config: SubsLlmConfig | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        config = config or SubsLlmConfig()
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
        if not assets.sub_paths:
            return PluginResult(status="abstain", reason="no embedded subtitles")

        srt_path = _first_srt(assets.sub_paths)
        if srt_path is None:
            return PluginResult(status="abstain", reason="PGS/VobSub OCR not supported in PoC")

        if not self.config.providers and not self.config.api_key and self.config.base_url == DEFAULT_BASE_URL:
            return PluginResult(status="abstain", reason="no LLM configured")

        srt_text = Path(srt_path).read_text(encoding="utf-8", errors="replace")
        cues = parse_srt(srt_text)[: self.config.max_cues]
        prompt = _build_prompt(ctx, claimed, cues)

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
                    "cue_count": len(cues),
                    "model": provider_model,
                    "provider": provider_name,
                },
            )
        except _LlmError as exc:
            logger.warning("subs-llm call failed: %s", exc)
            return PluginResult(status="error", reason=str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            logger.exception("subs-llm returned an unusable response")
            return PluginResult(status="error", reason=f"malformed LLM response: {exc}")
        except Exception as exc:
            # Belt-and-braces, matching whisper-subs: an identifier plugin
            # must never raise out of identify(), so any exception missed by
            # the specific handlers above (e.g. a pydantic ValidationError
            # from an empty `episodes` list) still maps to status="error".
            logger.exception("subs-llm identify failed")
            return PluginResult(status="error", reason=f"subs-llm failed: {exc}")

        return PluginResult(status="ok", candidates=candidates)
