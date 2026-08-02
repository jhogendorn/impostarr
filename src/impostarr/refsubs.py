"""Reference subtitle service.

Shared, provider-backed subtitle lookup used by identifier plugins. Lookup
order: manual drop-in directory (operator-supplied SRTs, always wins) ->
per-episode cache -> OpenSubtitles REST API (JWT-authenticated, quota-aware).

Never raises to callers: any failure (network, auth, quota, missing tvdb id,
no results) is logged and yields `None` so dependent plugins abstain instead
of crashing.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import RefSubsConfig

logger = logging.getLogger(__name__)

OPENSUBTITLES_BASE_URL = "https://api.opensubtitles.com/api/v1"


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

        cache_path = Path(self.cfg.cache_dir) / str(tvdb_id) / name
        if cache_path.exists():
            return cache_path

        if not self._quota_available():
            logger.info("reference subtitle daily quota exhausted, skipping API fetch")
            return None

        try:
            return await self._fetch_via_api(tvdb_id, season, episode, cache_path)
        except Exception:
            logger.exception("reference subtitle fetch failed unexpectedly")
            return None

    # -- quota --------------------------------------------------------

    def _quota_path(self) -> Path:
        return Path(self.cfg.cache_dir) / "quota.json"  # type: ignore[arg-type]

    def _load_quota(self) -> dict[str, Any]:
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        path = self._quota_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        return data

    def _quota_available(self) -> bool:
        return self._load_quota()["count"] < self.cfg.daily_quota

    def _increment_quota(self) -> None:
        data = self._load_quota()
        data["count"] += 1
        path = self._quota_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    # -- OpenSubtitles API ----------------------------------------------

    async def _fetch_via_api(
        self, tvdb_id: Any, season: int, episode: int, cache_path: Path
    ) -> Path | None:
        file_id = await self._search(tvdb_id, season, episode)
        if file_id is None:
            return None
        link = await self._download(file_id)
        if link is None:
            return None
        self._increment_quota()
        return await self._save(link, cache_path)

    def _headers(self) -> dict[str, str]:
        headers = {"Api-Key": self.cfg.api_key or ""}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _login(self) -> str | None:
        try:
            response = await self.http.post(
                f"{OPENSUBTITLES_BASE_URL}/login",
                json={"username": self.cfg.username, "password": self.cfg.password},
                headers={"Api-Key": self.cfg.api_key or ""},
            )
        except httpx.HTTPError:
            logger.exception("opensubtitles login request failed")
            return None
        if response.status_code >= 400:
            logger.warning(
                "opensubtitles login failed: %s %s", response.status_code, response.text
            )
            return None
        token = response.json().get("token")
        self._token = token
        return token

    async def _authed_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response | None:
        """Issue an authenticated request, logging in first if needed and
        retrying once (with a fresh login) on a 401."""
        if self._token is None and await self._login() is None:
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
            response = await self.http.get(link)
        except httpx.HTTPError:
            logger.exception("failed to fetch reference subtitle link: %s", link)
            return None
        if response.status_code >= 400:
            logger.warning("failed to fetch reference subtitle link: %s -> %s", link, response.status_code)
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        return cache_path
