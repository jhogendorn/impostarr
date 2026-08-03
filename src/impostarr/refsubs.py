"""Reference subtitle service.

Shared, provider-backed subtitle lookup used by identifier plugins. Lookup
order: manual drop-in directory (operator-supplied SRTs, always wins) ->
per-episode cache -> OpenSubtitles REST API (JWT-authenticated, quota-aware).

Never raises to callers: any failure (network, auth, quota, missing tvdb id,
no results) is logged and yields `None` so dependent plugins abstain instead
of crashing.
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

    async def get(self, series_ext_ids: dict[str, Any], season: int, episode: int) -> Path | None:
        tvdb_id = series_ext_ids.get("tvdb")
        if not tvdb_id:
            logger.warning("series_ext_ids has no tvdb id, cannot fetch reference subtitle")
            return None

        name = f"S{season:02d}E{episode:02d}.srt"

        if self.cfg.manual_dir:
            manual_path = Path(self.cfg.manual_dir) / str(tvdb_id) / name
            if manual_path.exists():
                return manual_path

        if not self.cfg.cache_dir:
            logger.warning("no cache_dir configured, cannot fetch reference subtitles")
            return None
        cache_dir = Path(self.cfg.cache_dir)

        cache_path = cache_dir / str(tvdb_id) / name
        if cache_path.exists():
            return cache_path

        if not await self._reserve_quota(cache_dir):
            logger.info("reference subtitle daily quota exhausted, skipping API fetch")
            return None

        # Reservation is provisional until /download actually succeeds
        # (the point OpenSubtitles debits its own remote quota) — released
        # below if the chain fails before that point, so a search miss or
        # transient error doesn't burn a real quota unit.
        committed = False
        try:
            file_id = await self._search(tvdb_id, season, episode)
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

    async def _login(self) -> str | None:
        """POST /login, retrying once (after a >=1.1s backoff) on a 429
        (`Login rate limit exceeded: 1 req/sec per IP`). Gives up cleanly
        (returns None) if the retry also 429s."""
        for attempt in range(2):
            try:
                response = await self.http.post(
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
        retrying once (with a fresh login) on a 401."""
        if not await self._ensure_login():
            return None

        try:
            response = await self.http.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError:
            logger.exception("opensubtitles request failed: %s %s", method, url)
            return None

        if response.status_code == 401:
            if await self._login() is None:
                return None
            try:
                response = await self.http.request(method, url, headers=self._headers(), **kwargs)
            except httpx.HTTPError:
                logger.exception("opensubtitles request failed after re-login: %s %s", method, url)
                return None

        if response.status_code >= 400:
            logger.warning(
                "opensubtitles request failed: %s %s -> %s", method, url, response.status_code
            )
            return None
        return response

    async def _search(self, tvdb_id: Any, season: int, episode: int) -> int | None:
        response = await self._authed_request(
            "GET",
            f"{OPENSUBTITLES_BASE_URL}/subtitles",
            params={
                "parent_tvdb_id": tvdb_id,
                "season_number": season,
                "episode_number": episode,
                "languages": "en",
            },
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

        if best_file_id is None:
            logger.info(
                "no reference subtitle results for tvdb=%s S%02dE%02d", tvdb_id, season, episode
            )
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
