"""Shared LLM chat-completions client with ordered-provider failover.

Extracted from `impostarr_plugin_subs_llm`'s original HTTP machinery so it
can be reused by any identifier plugin that talks to an OpenAI-compatible
`/chat/completions` endpoint (subs-llm today, transcript-llm next).

Failover contract: `chat_json` tries `providers` in order, falling through
to the next one ONLY when a provider looks unavailable/exhausted:
  - HTTP 401/402/403/429
  - a response body carrying an `insufficient_quota` error code (some
    OpenAI-compatible servers report quota exhaustion via other statuses,
    e.g. 400, so the body is sniffed regardless of status)
  - a persistent HTTP 5xx -- one retry is given first; only a 5xx on the
    retry too counts as "the provider is down"
  - a transport error (connection refused, timeout, ...)
Everything else -- any other HTTP error, or malformed/invalid-shape JSON
content even after the existing retry-with-reminder -- is a content
failure, not a provider failure: it does NOT fall through to the next
provider, it raises immediately so the caller can surface it as an error.
This distinction matters because a content failure is about what the
*chosen* provider replied, not whether it was reachable; silently retrying
a different provider on a "the model won't emit valid JSON" failure would
paper over a prompt/response-contract bug instead of surfacing it.

HTTP client lifecycle mirrors `RefSubService`/`SubsLlmPlugin`'s existing
pattern: an `httpx.AsyncClient` can be injected (caller owns it); otherwise
one is lazily created and owned here, closable via `aclose()`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

from impostarr.plugins.base import Candidate, CandidateIdent

logger = logging.getLogger(__name__)

_JSON_REMINDER = "Your previous reply was not valid JSON. Reply with valid JSON only."
_UNAVAILABLE_STATUSES = frozenset({401, 402, 403, 429})


class LlmProvider(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""


class LlmUnavailable(RuntimeError):
    """The provider looks unavailable/exhausted (auth/quota/429, a
    persistent 5xx, or a transport error) -- `chat_json` falls through to
    the next configured provider on this."""


class LlmContentError(RuntimeError):
    """The chosen provider replied, but its content couldn't be turned
    into usable JSON (any other HTTP error, or invalid JSON/shape even
    after the retry-with-reminder) -- `chat_json` does NOT fall through
    on this; it's a failure of that response, not of provider reachability."""


def build_episode_candidates(
    llm_season: int,
    llm_episodes: list[int],
    llm_confidence: float,
    claimed_season: int,
    claimed_episodes: list[int],
    llm_evidence: dict[str, Any],
) -> list[Candidate]:
    """Shared claimed/derived candidate-building logic for LLM episode-
    identification plugins (subs-llm, transcript-llm): if the LLM's answer
    already IS the claimed episode, that's the only candidate returned;
    otherwise the LLM's answer becomes one candidate and a second, derived
    candidate represents "the claimed episode, at low confidence"
    (1 - llm_confidence) -- so the claimed ident is always represented."""
    llm_candidate = Candidate(
        confidence=llm_confidence,
        ident=CandidateIdent(series="claimed", season=llm_season, episodes=llm_episodes),
        numbering="tvdb",
        evidence=llm_evidence,
    )
    identified_claimed = llm_season == claimed_season and set(llm_episodes) == set(claimed_episodes)
    if identified_claimed:
        return [llm_candidate]
    claimed_candidate = Candidate(
        confidence=max(0.0, 1.0 - llm_confidence),
        ident=CandidateIdent(series="claimed", season=claimed_season, episodes=list(claimed_episodes)),
        numbering="tvdb",
        evidence={"source": "derived"},
    )
    return [llm_candidate, claimed_candidate]


def episode_json_valid(data: dict[str, Any]) -> bool:
    """Shape validator for the shared `{season, episodes, confidence,
    reasoning}` episode-identification JSON contract, used by both
    subs-llm and transcript-llm as `chat_json`'s `validate` callback --
    lives here (rather than in either plugin package) so both can use it
    without a disallowed cross-plugin import. A syntactically-valid reply
    missing the required keys (or with `episodes` not a list) triggers the
    retry-with-reminder rather than failing outright."""
    return "season" in data and "episodes" in data and isinstance(data["episodes"], list)


def _is_insufficient_quota_body(text: str) -> bool:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or error.get("type") or "")
    return "insufficient_quota" in code


class LlmClient:
    def __init__(
        self,
        providers: list[LlmProvider],
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 60,
    ) -> None:
        self.providers = providers
        self.timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

    async def aclose(self) -> None:
        """Closes the lazily-created owned client, if any. No-op when an
        `httpx.AsyncClient` was injected -- the injector owns that
        lifecycle."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_s)
        return self._http

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        validate: Callable[[dict[str, Any]], bool] | None = None,
        max_attempts: int = 2,
    ) -> tuple[dict[str, Any], str]:
        """Tries each provider in order; returns `(parsed_content, provider_name)`
        from the first one that answers with usable JSON. Raises
        `LlmUnavailable` (all providers exhausted) or `LlmContentError`
        (the chosen provider replied but its content was unusable, even
        after retry) -- see the module docstring for the fallthrough
        contract."""
        if not self.providers:
            raise LlmUnavailable("no LLM providers configured")

        last_exc: LlmUnavailable | None = None
        for provider in self.providers:
            try:
                data = await self._call_provider(provider, messages, validate, max_attempts)
            except LlmUnavailable as exc:
                logger.warning("llm provider %r unavailable, trying next: %s", provider.name, exc)
                last_exc = exc
                continue
            return data, provider.name
        assert last_exc is not None
        raise last_exc

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        provider: LlmProvider,
        convo: list[dict[str, str]],
    ) -> httpx.Response:
        try:
            return await client.post(
                url,
                headers=headers,
                json={
                    "model": provider.model,
                    "messages": convo,
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"{provider.name}: transport error: {exc}") from exc

    async def _post_with_5xx_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        provider: LlmProvider,
        convo: list[dict[str, str]],
    ) -> httpx.Response:
        response = await self._post(client, url, headers, provider, convo)
        if response.status_code >= 500:
            response = await self._post(client, url, headers, provider, convo)
            if response.status_code >= 500:
                raise LlmUnavailable(f"{provider.name}: persistent HTTP {response.status_code}")
        return response

    async def _call_provider(
        self,
        provider: LlmProvider,
        messages: list[dict[str, str]],
        validate: Callable[[dict[str, Any]], bool] | None,
        max_attempts: int,
    ) -> dict[str, Any]:
        client = await self._get_http()
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        convo = list(messages)

        for _attempt in range(max_attempts):
            response = await self._post_with_5xx_retry(client, url, headers, provider, convo)

            if response.status_code in _UNAVAILABLE_STATUSES:
                raise LlmUnavailable(f"{provider.name}: HTTP {response.status_code}")
            if _is_insufficient_quota_body(response.text):
                raise LlmUnavailable(f"{provider.name}: insufficient quota")
            if response.status_code != 200:
                raise LlmContentError(f"{provider.name}: LLM request failed: HTTP {response.status_code}")

            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise LlmContentError(f"{provider.name}: LLM response malformed: {exc}") from exc

            try:
                data = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                data = None

            if isinstance(data, dict) and (validate is None or validate(data)):
                return data

            # Include the model's own invalid reply before the reminder --
            # small models self-correct better when they can see what they
            # just said.
            convo = [
                *convo,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _JSON_REMINDER},
            ]

        raise LlmContentError(f"{provider.name}: LLM did not return valid JSON after retry")
