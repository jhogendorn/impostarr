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

SRT parsing reuses `whisper_subs.parse_srt` (same minimal cue parser; no
need for a second implementation in-package).

HTTP client lifecycle: mirrors `RefSubService`'s injection pattern — an
`httpx.AsyncClient` can be passed to the constructor (caller owns it, e.g.
Task 14's pipeline sharing one client across plugins); otherwise the plugin
lazily creates and owns one on first use, closable via `aclose()`.

Retry: on a malformed-JSON reply, the retry appends the model's own invalid
reply as an `assistant` turn before the reminder `user` turn — small models
self-correct better when they can see what they just said.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .base import (
    AssetBundle,
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)
from .whisper_subs import parse_srt

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_JSON_REMINDER = "Your previous reply was not valid JSON. Reply with valid JSON only."
_MAX_ATTEMPTS = 2


class SubsLlmConfig(BaseModel):
    base_url: str = DEFAULT_BASE_URL
    model: str = "gpt-4o-mini"
    api_key: str = ""
    max_cues: int = Field(default=80, ge=1)
    timeout_s: float = Field(default=60, gt=0)


class _LlmError(RuntimeError):
    """Raised for any LLM-call failure (HTTP error, transport error, or
    malformed JSON after the retry); caught once in `identify` and mapped to
    a PluginResult(status="error")."""


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


def _parse_llm_json(content: str) -> dict[str, Any] | None:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "season" not in data or "episodes" not in data:
        return None
    if not isinstance(data["episodes"], list):
        return None
    return data


class SubsLlmPlugin(IdentifierPlugin):
    name = "subs-llm"
    version = "1.0.0"
    config_model = SubsLlmConfig

    def __init__(
        self, config: SubsLlmConfig | None = None, http: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(config or SubsLlmConfig())
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
            self._http = httpx.AsyncClient(timeout=self.config.timeout_s)
        return self._http

    async def _call_llm(self, prompt: str) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        client = await self._get_http()

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={
                        "model": self.config.model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                    },
                )
            except httpx.HTTPError as exc:
                raise _LlmError(f"LLM request failed: {exc}") from exc

            if resp.status_code != 200:
                raise _LlmError(f"LLM request failed: HTTP {resp.status_code}")

            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise _LlmError(f"LLM response malformed: {exc}") from exc

            parsed = _parse_llm_json(content)
            if parsed is not None:
                return parsed

            # Include the model's own invalid reply before the reminder —
            # small models self-correct better seeing what they just said.
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _JSON_REMINDER},
            ]

        raise _LlmError("LLM did not return valid JSON after retry")

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        if not assets.sub_paths:
            return PluginResult(status="abstain", reason="no embedded subtitles")

        srt_path = _first_srt(assets.sub_paths)
        if srt_path is None:
            return PluginResult(status="abstain", reason="PGS/VobSub OCR not supported in PoC")

        if not self.config.api_key and self.config.base_url == DEFAULT_BASE_URL:
            return PluginResult(status="abstain", reason="no LLM configured")

        srt_text = Path(srt_path).read_text(encoding="utf-8", errors="replace")
        cues = parse_srt(srt_text)[: self.config.max_cues]
        prompt = _build_prompt(ctx, claimed, cues)

        try:
            data = await self._call_llm(prompt)
            llm_season = int(data["season"])
            llm_episodes = [int(e) for e in data["episodes"]]
            llm_confidence = _clamp01(float(data.get("confidence", 0.0)))
            reasoning = str(data.get("reasoning", ""))

            llm_candidate = Candidate(
                confidence=llm_confidence,
                ident=CandidateIdent(series="claimed", season=llm_season, episodes=llm_episodes),
                numbering="tvdb",
                evidence={
                    "reasoning": reasoning,
                    "cue_count": len(cues),
                    "model": self.config.model,
                },
            )

            identified_claimed = llm_season == claimed.season and set(llm_episodes) == set(
                claimed.episodes
            )
            if identified_claimed:
                candidates = [llm_candidate]
            else:
                claimed_candidate = Candidate(
                    confidence=max(0.0, 1.0 - llm_confidence),
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=list(claimed.episodes)
                    ),
                    numbering="tvdb",
                    evidence={"source": "derived"},
                )
                candidates = [llm_candidate, claimed_candidate]
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
