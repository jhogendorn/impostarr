"""Async client for the Sonarr v3 API.

One class, async only, no auth beyond the `X-Api-Key` header. Callers adapt
`SonarrInstance` config into `(base_url, api_key)` — this module has no
dependency on `impostarr.config`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from .types import Episode, EpisodeFile, HistoryRecord, ManualImportItem, Series, SystemStatus

DEFAULT_BACKOFF: tuple[float, ...] = (0.5, 1.0, 2.0)
DEFAULT_TIMEOUT = httpx.Timeout(30)
HISTORY_PAGE_SIZE = 100


class SonarrError(Exception):
    """Raised for non-2xx responses from the Sonarr API."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Sonarr API error {status_code}: {body}")


class SonarrClient:
    """Typed async client for the Sonarr v3 API.

    Retries 5xx responses and httpx transport errors/timeouts up to
    `max_retries` times with delays from `backoff` (exponential by default,
    injectable/zero-able for tests). 4xx responses raise `SonarrError`
    immediately, never retried.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v3",
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )
        self._max_retries = max_retries
        self._backoff = backoff

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1
                continue

            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise SonarrError(response.status_code, response.text)
                await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1
                continue

            if response.status_code >= 400:
                raise SonarrError(response.status_code, response.text)

            return response

    async def system_status(self) -> SystemStatus:
        response = await self._request("GET", "/system/status")
        return SystemStatus.model_validate(response.json())

    async def history_since(self, history_id: int) -> list[HistoryRecord]:
        """Fetch new history records newer than `history_id`.

        Pages newest-first (`sortDirection=descending`) so a poll only walks
        the handful of pages since the last watermark instead of Sonarr's
        entire lifetime history: stops as soon as a page yields a record at
        or below the watermark (everything after it, on this page and any
        further page, is older). Returned records are re-sorted ascending by
        id, since callers rely on ascending order.
        """
        records: list[HistoryRecord] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                "/history",
                params={
                    "eventType": "downloadFolderImported",
                    "sortKey": "id",
                    "sortDirection": "descending",
                    "pageSize": HISTORY_PAGE_SIZE,
                    "page": page,
                },
            )
            batch = response.json().get("records", [])
            if not batch:
                break
            reached_watermark = False
            for raw in batch:
                if raw["id"] <= history_id:
                    reached_watermark = True
                    break
                records.append(HistoryRecord.model_validate(raw))
            if reached_watermark:
                break
            page += 1
        records.sort(key=lambda r: r.id)
        return records

    async def episode_files(self, series_id: int) -> list[EpisodeFile]:
        response = await self._request("GET", "/episodefile", params={"seriesId": series_id})
        return [EpisodeFile.model_validate(raw) for raw in response.json()]

    async def episode_file(self, file_id: int) -> EpisodeFile:
        response = await self._request("GET", f"/episodefile/{file_id}")
        return EpisodeFile.model_validate(response.json())

    async def series(self, series_id: int) -> Series:
        response = await self._request("GET", f"/series/{series_id}")
        return Series.model_validate(response.json())

    async def all_series(self) -> list[Series]:
        response = await self._request("GET", "/series")
        return [Series.model_validate(raw) for raw in response.json()]

    async def episodes(self, series_id: int) -> list[Episode]:
        response = await self._request("GET", "/episode", params={"seriesId": series_id})
        return [Episode.model_validate(raw) for raw in response.json()]

    async def delete_episode_file(self, file_id: int) -> None:
        await self._request("DELETE", f"/episodefile/{file_id}")

    async def mark_history_failed(self, history_id: int) -> None:
        await self._request("POST", f"/history/failed/{history_id}")

    async def manual_import_candidates(self, folder: str) -> list[ManualImportItem]:
        response = await self._request(
            "GET",
            "/manualimport",
            params={"folder": folder, "filterExistingFiles": "false"},
        )
        return [ManualImportItem.model_validate(raw) for raw in response.json()]

    async def execute_manual_import(
        self, files: list[dict[str, Any]], import_mode: str = "move"
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/command",
            json={"name": "ManualImport", "files": files, "importMode": import_mode},
        )
        return response.json()

    async def command(self, name: str, **body: Any) -> dict[str, Any]:
        response = await self._request("POST", "/command", json={"name": name, **body})
        return response.json()
