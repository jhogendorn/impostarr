"""Reference subtitle service.

Shared, provider-backed subtitle lookup used by identifier plugins. Lookup
order: manual drop-in directory (operator-supplied SRTs, always wins) ->
per-episode cache -> OpenSubtitles REST API (JWT-authenticated, quota-aware).

Never raises to callers: any failure (network, auth, quota, missing tvdb id,
no results) is logged and yields `None` so dependent plugins abstain instead
of crashing.

Language awareness: `get()` accepts an optional `language` (ISO 639-1, e.g.
a transcript's detected language). Languages are tried in order: `language`
first (if given), then `RefSubsConfig.languages` (deduped against
`language`) -- e.g. whisper-subs compares a Japanese-audio transcript
against Japanese subs first rather than always assuming English. Each
language is tried end-to-end (manual dir -> cache -> API) before moving to
the next, mirroring `_search`'s imdb -> tvdb -> title fallback shape.

Cache paths are language-scoped (`<cache>/<tvdb>/SxxEyy.<lang>.srt`) so
different languages for the same episode don't collide. The manual
drop-in directory checks the language-suffixed name first, then falls back
to the legacy unsuffixed name (`SxxEyy.srt`) for back-compat with existing
manual drop-ins predating language awareness.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .config import RefSubsConfig

logger = logging.getLogger(__name__)

OPENSUBTITLES_BASE_URL = "https://api.opensubtitles.com/api/v1"

# OpenSubtitles' gateway (kong) 403s requests with no identifying
# User-Agent (`kong-user-agent-block`) -- sent on every request, including
# the off-API CDN link fetch.
USER_AGENT = f"Impostarr/{__version__} (github.com/jhogendorn/impostarr)"


class RefSubService:
    """Fetches a reference subtitle for one episode, caching it on disk.

    `http` is an injected `httpx.AsyncClient` (no base_url assumed — full
    URLs are used throughout, since download links point off-API to a CDN).
    The OpenSubtitles JWT is cached in-memory for this instance's lifetime
    and refreshed once on a 401.
    """

    def __init__(self, cfg: RefSubsConfig, http: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.http = http
        self._token: str | None = None
        # Guards check-and-reserve of the daily quota so two concurrent
        # get() calls near the limit can't both pass the check before
        # either increments (the check-then-network-then-increment window
        # otherwise spans several awaits).
        self._quota_lock = asyncio.Lock()
        # Single-flights login acquisition: whisper-subs gathers up to ~7
        # get() calls concurrently, each of which would otherwise see
        # `_token is None` and independently POST /login, tripping
        # OpenSubtitles' 1 req/sec per IP login rate limit.
        self._login_lock = asyncio.Lock()
        # Paces every OpenSubtitles API call (login, search, download) so
        # concurrent get() callers queue instead of stampeding -- serializes
        # (only one call in flight at a time) AND enforces a minimum gap
        # between successive call starts. The CDN link fetch in _save() is
        # NOT paced through this: it's a different host, not subject to
        # OpenSubtitles' API rate limit.
        self._pacing_lock = asyncio.Lock()
        self._last_request_at: float | None = None

    def _language_order(self, language: str | None) -> list[str]:
        """`language` first (if given), then `cfg.languages`, deduped
        while preserving order."""
        ordered = [language] if language else []
        for lang in self.cfg.languages:
            if lang not in ordered:
                ordered.append(lang)
        return ordered

    @staticmethod
    def _srt_name(season: int, episode: int, language: str | None = None) -> str:
        base = f"S{season:02d}E{episode:02d}"
        return f"{base}.{language}.srt" if language else f"{base}.srt"

    def _manual_path(self, tvdb_id: Any, season: int, episode: int, langs: list[str]) -> Path | None:
        if not self.cfg.manual_dir:
            return None
        manual_dir = Path(self.cfg.manual_dir) / str(tvdb_id)
        for lang in langs:
            path = manual_dir / self._srt_name(season, episode, lang)
            if path.exists():
                return path
        # Legacy unsuffixed name, predating language awareness.
        legacy_path = manual_dir / self._srt_name(season, episode)
        if legacy_path.exists():
            return legacy_path
        return None

    def quota_status(self) -> dict[str, int] | None:
        """Current daily OpenSubtitles quota usage, for surfacing in
        `GET /status` (`refsubs_quota`). `None` when no `cache_dir` is
        configured -- quota tracking has nowhere to persist its counter in
        that case (see `get()`)."""
        if not self.cfg.cache_dir:
            return None
        data = self._load_quota(Path(self.cfg.cache_dir))
        return {"used": data.get("count", 0), "limit": self.cfg.daily_quota}

    async def get(
        self, series_ext_ids: dict[str, Any], season: int, episode: int, language: str | None = None
    ) -> Path | None:
        tvdb_id = series_ext_ids.get("tvdb")
        if not tvdb_id:
            logger.warning("series_ext_ids has no tvdb id, cannot fetch reference subtitle")
            return None

        langs = self._language_order(language)

        manual_hit = self._manual_path(tvdb_id, season, episode, langs)
        if manual_hit is not None:
            return manual_hit

        if not self.cfg.cache_dir:
            logger.warning("no cache_dir configured, cannot fetch reference subtitles")
            return None
        cache_dir = Path(self.cfg.cache_dir)

        # Each language is tried to exhaustion (cache, then a live API
        # fetch) before moving to the next -- a cached fallback-language
        # file must not silently pre-empt fetching the preferred language,
        # or the whole point of language awareness (comparing against the
        # right language) is defeated.
        for lang in langs:
            cache_path = cache_dir / str(tvdb_id) / self._srt_name(season, episode, lang)
            if cache_path.exists():
                return cache_path
            result = await self._fetch(series_ext_ids, season, episode, lang, cache_dir, cache_path)
            if result is not None:
                return result
        return None

    async def _fetch(
        self,
        series_ext_ids: dict[str, Any],
        season: int,
        episode: int,
        language: str,
        cache_dir: Path,
        cache_path: Path,
    ) -> Path | None:
        """One end-to-end API fetch attempt for a single language: quota
        reservation, search, download, save. Mirrors the pre-language-aware
        `get()` body exactly, just parameterized on `language`."""
        if not await self._reserve_quota(cache_dir):
            logger.info("reference subtitle daily quota exhausted, skipping API fetch")
            return None

        # Reservation is provisional until /download actually succeeds
        # (the point OpenSubtitles debits its own remote quota) — released
        # below if the chain fails before that point, so a search miss or
        # transient error doesn't burn a real quota unit.
        committed = False
        try:
            file_id = await self._search(series_ext_ids, season, episode, language)
            if file_id is None:
                return None
            link = await self._download(file_id)
            if link is None:
                return None
            committed = True
            return await self._save(link, cache_path)
        except Exception:
            logger.exception("reference subtitle fetch failed unexpectedly")
            return None
        finally:
            if not committed:
                await self._release_quota(cache_dir)

    # -- quota --------------------------------------------------------

    def _quota_path(self, cache_dir: Path) -> Path:
        return cache_dir / "quota.json"

    def _load_quota(self, cache_dir: Path) -> dict[str, Any]:
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        path = self._quota_path(cache_dir)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        return data

    def _write_quota(self, cache_dir: Path, data: dict[str, Any]) -> None:
        path = self._quota_path(cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    async def _reserve_quota(self, cache_dir: Path) -> bool:
        """Atomically check-and-increment; False without writing if at quota."""
        async with self._quota_lock:
            data = self._load_quota(cache_dir)
            if data["count"] >= self.cfg.daily_quota:
                return False
            data["count"] += 1
            self._write_quota(cache_dir, data)
            return True

    async def _release_quota(self, cache_dir: Path) -> None:
        """Roll back a reservation that didn't reach a real remote download."""
        async with self._quota_lock:
            data = self._load_quota(cache_dir)
            data["count"] = max(0, data["count"] - 1)
            self._write_quota(cache_dir, data)

    # -- OpenSubtitles API ----------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Api-Key": self.cfg.api_key or "", "User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _paced_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issues one OpenSubtitles API request, serialized against every
        other call through `_pacing_lock` and spaced at least
        `min_request_interval_s` after the previous call's start. Held for
        the full request duration, so calls queued behind it never overlap
        in flight either. May raise `httpx.HTTPError`; callers already
        catch that around each call site."""
        async with self._pacing_lock:
            loop = asyncio.get_running_loop()
            if self._last_request_at is not None:
                wait = self.cfg.min_request_interval_s - (loop.time() - self._last_request_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = loop.time()
            return await self.http.request(method, url, **kwargs)

    async def _login(self) -> str | None:
        """POST /login, retrying once (after a >=1.1s backoff) on a 429
        (`Login rate limit exceeded: 1 req/sec per IP`). Gives up cleanly
        (returns None) if the retry also 429s."""
        for attempt in range(2):
            try:
                response = await self._paced_request(
                    "POST",
                    f"{OPENSUBTITLES_BASE_URL}/login",
                    json={"username": self.cfg.username, "password": self.cfg.password},
                    headers={"Api-Key": self.cfg.api_key or "", "User-Agent": USER_AGENT},
                )
            except httpx.HTTPError:
                logger.exception("opensubtitles login request failed")
                return None
            if response.status_code == 429:
                if attempt == 0:
                    logger.warning("opensubtitles login rate-limited, backing off and retrying once")
                    await asyncio.sleep(1.1)
                    continue
                logger.warning("opensubtitles login rate-limited on retry, giving up")
                return None
            if response.status_code >= 400:
                logger.warning(
                    "opensubtitles login failed: %s %s", response.status_code, response.text
                )
                return None
            token = response.json().get("token")
            self._token = token
            return token
        return None

    async def _ensure_login(self) -> bool:
        """Single-flights login acquisition: concurrent callers wait on the
        same lock and re-check `_token` after acquiring it, so only one of
        them actually performs the login."""
        if self._token is not None:
            return True
        async with self._login_lock:
            if self._token is not None:
                return True
            return await self._login() is not None

    async def _authed_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response | None:
        """Issue an authenticated request, logging in first if needed and
        retrying once (with a fresh login) on a 401. Also retries once
        (after a 2s backoff) on a 429 from search/download -- concurrent
        get() callers used to stampede these endpoints with no pacing at
        all, producing exactly this in production."""
        if not await self._ensure_login():
            return None

        for attempt in range(2):
            try:
                response = await self._paced_request(method, url, headers=self._headers(), **kwargs)
            except httpx.HTTPError:
                logger.exception("opensubtitles request failed: %s %s", method, url)
                return None

            if response.status_code == 401:
                if await self._login() is None:
                    return None
                try:
                    response = await self._paced_request(method, url, headers=self._headers(), **kwargs)
                except httpx.HTTPError:
                    logger.exception("opensubtitles request failed after re-login: %s %s", method, url)
                    return None

            if response.status_code == 429:
                if attempt == 0:
                    logger.warning(
                        "opensubtitles request rate-limited, backing off and retrying once: %s %s",
                        method,
                        url,
                    )
                    await asyncio.sleep(2.0)
                    continue
                logger.warning("opensubtitles request rate-limited on retry, giving up: %s %s", method, url)
                return None

            if response.status_code >= 400:
                logger.warning(
                    "opensubtitles request failed: %s %s -> %s", method, url, response.status_code
                )
                return None
            return response
        return None

    @staticmethod
    def _numeric_imdb_id(raw: Any) -> int | None:
        """Normalizes an imdb id (`"tt1086788"`, `"1086788"`, or an int) to
        the bare numeric id OpenSubtitles' `parent_imdb_id` expects."""
        if raw is None:
            return None
        text = str(raw).removeprefix("tt")
        return int(text) if text.isdigit() else None

    async def _search(
        self, series_ext_ids: dict[str, Any], season: int, episode: int, language: str
    ) -> int | None:
        """Tries `parent_imdb_id` -> `parent_tvdb_id` -> `query` (series
        title), moving to the next strategy whenever one comes back empty
        (id unavailable, 4xx/5xx, or zero results) -- OpenSubtitles' tvdb
        search path has been flaky/unsupported in production regardless of
        param order, so imdb is preferred when available."""
        imdb_id = self._numeric_imdb_id(series_ext_ids.get("imdb"))
        if imdb_id is not None:
            file_id = await self._search_query(
                {
                    "episode_number": episode,
                    "languages": language,
                    "parent_imdb_id": imdb_id,
                    "season_number": season,
                }
            )
            if file_id is not None:
                logger.info("reference subtitle search served by imdb strategy: S%02dE%02d", season, episode)
                return file_id

        tvdb_id = series_ext_ids.get("tvdb")
        if tvdb_id is not None:
            file_id = await self._search_query(
                {
                    "episode_number": episode,
                    "languages": language,
                    "parent_tvdb_id": tvdb_id,
                    "season_number": season,
                }
            )
            if file_id is not None:
                logger.info("reference subtitle search served by tvdb strategy: S%02dE%02d", season, episode)
                return file_id

        title = series_ext_ids.get("title")
        if title:
            file_id = await self._search_query(
                {
                    "episode_number": episode,
                    "languages": language,
                    "query": title,
                    "season_number": season,
                }
            )
            if file_id is not None:
                logger.info("reference subtitle search served by query strategy: S%02dE%02d", season, episode)
                return file_id

        logger.info("no reference subtitle results from any search strategy: S%02dE%02d", season, episode)
        return None

    async def _search_query(self, params: dict[str, Any]) -> int | None:
        """Issues one `/subtitles` search. Params are sent in alphabetical
        key order -- OpenSubtitles treats that as canonical and 301s
        anything else (handled by the client's `follow_redirects=True`,
        but sending canonical order avoids the redirect round-trip)."""
        response = await self._authed_request(
            "GET",
            f"{OPENSUBTITLES_BASE_URL}/subtitles",
            params=dict(sorted(params.items())),
        )
        if response is None:
            return None

        best_file_id = None
        best_download_count = -1
        for item in response.json().get("data", []):
            attributes = item.get("attributes", {})
            files = attributes.get("files") or []
            if not files:
                continue
            download_count = attributes.get("download_count", 0)
            if download_count > best_download_count:
                best_download_count = download_count
                best_file_id = files[0].get("file_id")
        return best_file_id

    async def _download(self, file_id: int) -> str | None:
        response = await self._authed_request(
            "POST", f"{OPENSUBTITLES_BASE_URL}/download", json={"file_id": file_id}
        )
        if response is None:
            return None
        link = response.json().get("link")
        if not link:
            logger.warning("opensubtitles download response missing link")
            return None
        return link

    async def _save(self, link: str, cache_path: Path) -> Path | None:
        try:
            response = await self.http.get(link, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError:
            logger.exception("failed to fetch reference subtitle link: %s", link)
            return None
        if response.status_code >= 400:
            logger.warning("failed to fetch reference subtitle link: %s -> %s", link, response.status_code)
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory then rename, so a
        # concurrent reader (or a crash mid-write) never observes a
        # partially-written subtitle at the final cache path.
        fd, tmp_name = tempfile.mkstemp(
            dir=cache_path.parent, prefix=f".{cache_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(response.content)
            os.replace(tmp_name, cache_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return cache_path
