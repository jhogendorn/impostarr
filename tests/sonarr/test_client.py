import json
from pathlib import Path

import httpx
import pytest
import respx

from impostarr.sonarr import SonarrClient, SonarrError

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://sonarr.test:8989"
API_URL = f"{BASE_URL}/api/v3"
API_KEY = "test-api-key"

# Fixed history query params (page varies per call, so it's checked separately).
# eventType=3 is Sonarr's numeric HistoryEventType.downloadFolderImported —
# the string form 400s. sortKey=date because Sonarr doesn't honor
# sortKey=id (silently substitutes date server-side).
HISTORY_PARAMS = {
    "eventType": "3",
    "sortKey": "date",
    "sortDirection": "descending",
    "pageSize": "100",
}


def history_record_raw(id_, *, episode_id=555, series_id=42, date="2026-07-30T10:15:00Z"):
    return {
        "id": id_,
        "episodeId": episode_id,
        "seriesId": series_id,
        "sourceTitle": "Show.Name.S01E02.1080p.WEB-DL",
        "downloadId": "abcdef0123456789",
        "eventType": "downloadFolderImported",
        "date": date,
        "quality": {"quality": {"id": 7, "name": "WEBDL-1080p"}},
        "languages": [{"id": 1, "name": "English"}],
        "data": {"fileId": "9001", "guid": "abcdef0123456789", "indexer": "NZBgeek"},
    }


def history_page_raw(records: list[dict]) -> dict:
    return {
        "page": 1,
        "pageSize": 100,
        "sortKey": "date",
        "sortDirection": "descending",
        "totalRecords": len(records),
        "records": records,
    }


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@respx.mock
async def test_system_status_happy_path():
    route = respx.get(f"{API_URL}/system/status").mock(
        return_value=httpx.Response(200, json=load_fixture("system_status.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        status = await client.system_status()

    assert status.version == "3.0.10.1567"
    assert route.calls.last.request.headers["X-Api-Key"] == API_KEY


@respx.mock
async def test_history_since_happy_path():
    # Single page (ids 102, 101 — newest first) with nothing at/below the
    # watermark, followed by an empty page that ends pagination normally.
    route = respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("history_page.json")),
            httpx.Response(200, json=load_fixture("history_page_empty.json")),
        ]
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        records = await client.history_since(0)

    # Results are re-sorted ascending for callers regardless of the
    # newest-first wire order.
    assert [r.id for r in records] == [101, 102]
    first = records[0]
    assert first.episode_ids == [555]
    assert first.series_id == 42
    assert first.source_title == "Show.Name.S01E02.1080p.WEB-DL"
    assert first.download_id == "abcdef0123456789"
    assert first.episode_file_id == 9001
    assert first.guid == "abcdef0123456789"
    assert first.indexer == "NZBgeek"
    assert route.call_count == 2
    assert route.calls[0].request.url.params["page"] == "1"
    assert route.calls[1].request.url.params["page"] == "2"


@respx.mock
async def test_history_since_stops_paging_when_page_has_zero_new_records():
    # The single mocked page (ids 102, 101 descending) has nothing above the
    # watermark (102): every record on it is at/below history_id. Under the
    # full-page-scan rule, pagination stops right there — a second page must
    # never be requested.
    route = respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(
        side_effect=[httpx.Response(200, json=load_fixture("history_page.json"))]
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        records = await client.history_since(102)

    assert records == []
    assert route.call_count == 1
    assert route.calls[0].request.url.params["page"] == "1"


@respx.mock
async def test_history_since_captures_interleaved_new_record_and_keeps_paging():
    # Sonarr actually sorts by date, not id, so a page can interleave a
    # record at/below the watermark BEFORE a newer one. The old "stop at
    # the first id <= watermark" rule would have missed id=105 here. The
    # full-page scan must still capture it, and since the page had at
    # least one new record, paging must continue to page 2 (which also has
    # one new record, id=106) and only stop at the empty page 3.
    watermark = 100
    route = respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(
        side_effect=[
            httpx.Response(200, json=history_page_raw([history_record_raw(99), history_record_raw(105)])),
            httpx.Response(200, json=history_page_raw([history_record_raw(106)])),
            httpx.Response(200, json=load_fixture("history_page_empty.json")),
        ]
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        records = await client.history_since(watermark)

    assert [r.id for r in records] == [105, 106]
    assert route.call_count == 3
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2", "3"]


@respx.mock
async def test_history_since_paginates_across_pages():
    # Watermark below all available ids, so no page hits the boundary;
    # pagination continues (newest-first) until an empty page is returned.
    pages = {
        "1": load_fixture("history_page2.json"),  # ids 104, 103
        "2": load_fixture("history_page.json"),  # ids 102, 101
        "3": load_fixture("history_page_empty.json"),
    }

    def responder(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        return httpx.Response(200, json=pages[page])

    route = respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(side_effect=responder)

    async with SonarrClient(BASE_URL, API_KEY) as client:
        records = await client.history_since(100)

    assert [r.id for r in records] == [101, 102, 103, 104]
    assert route.call_count == 3
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2", "3"]


@respx.mock
async def test_history_since_first_page_empty():
    route = respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(
        side_effect=[httpx.Response(200, json=load_fixture("history_page_empty.json"))]
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        records = await client.history_since(0)

    assert records == []
    assert route.call_count == 1


@respx.mock
async def test_episode_files_happy_path():
    route = respx.get(f"{API_URL}/episodefile").mock(
        return_value=httpx.Response(200, json=load_fixture("episode_files.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        files = await client.episode_files(42)

    assert [f.id for f in files] == [9001, 9002]
    assert files[0].path == "/tv/Show Name/Season 01/Show Name - S01E02 - Title.mkv"
    assert files[0].size == 1073741824
    assert route.calls.last.request.url.params["seriesId"] == "42"


@respx.mock
async def test_episode_file_happy_path():
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=load_fixture("episode_file.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        file = await client.episode_file(9001)

    assert file.id == 9001
    assert file.series_id == 42


@respx.mock
async def test_series_happy_path():
    respx.get(f"{API_URL}/series/42").mock(
        return_value=httpx.Response(200, json=load_fixture("series.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        series = await client.series(42)

    assert series.id == 42
    assert series.title == "Show Name"
    assert series.tvdb_id == 123456
    assert series.imdb_id == "tt1234567"
    assert series.tmdb_id == 654321
    assert series.title_slug == "show-name"


@respx.mock
async def test_all_series_happy_path():
    respx.get(f"{API_URL}/series").mock(
        return_value=httpx.Response(200, json=load_fixture("all_series.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        series = await client.all_series()

    assert [s.id for s in series] == [42, 43]
    assert series[1].tmdb_id is None


@respx.mock
async def test_episodes_happy_path():
    route = respx.get(f"{API_URL}/episode").mock(
        return_value=httpx.Response(200, json=load_fixture("episodes.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        episodes = await client.episodes(42)

    assert [e.id for e in episodes] == [555, 556, 557]
    scene = episodes[1]
    assert scene.scene_season_number == 1
    assert scene.scene_episode_number == 4
    assert scene.scene_absolute_episode_number == 4
    no_file = episodes[2]
    assert no_file.has_file is False
    assert route.calls.last.request.url.params["seriesId"] == "42"


@respx.mock
async def test_delete_episode_file():
    route = respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200))
    async with SonarrClient(BASE_URL, API_KEY) as client:
        await client.delete_episode_file(9001)

    assert route.called


@respx.mock
async def test_mark_history_failed():
    route = respx.post(f"{API_URL}/history/failed/101").mock(return_value=httpx.Response(200))
    async with SonarrClient(BASE_URL, API_KEY) as client:
        await client.mark_history_failed(101)

    assert route.called


@respx.mock
async def test_manual_import_candidates_happy_path():
    route = respx.get(f"{API_URL}/manualimport").mock(
        return_value=httpx.Response(200, json=load_fixture("manual_import.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY) as client:
        items = await client.manual_import_candidates("/staging")

    assert route.calls.last.request.url.params["folder"] == "/staging"
    assert route.calls.last.request.url.params["filterExistingFiles"] == "false"
    assert len(items) == 2
    assert items[0].series == {"id": 42, "title": "Show Name"}
    assert items[1].series is None
    assert items[1].rejections[0]["reason"] == "Unable to parse file"


@respx.mock
async def test_execute_manual_import():
    route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json=load_fixture("command_response.json"))
    )
    files = [{"path": "/staging/x.mkv", "episodeIds": [555]}]

    async with SonarrClient(BASE_URL, API_KEY) as client:
        result = await client.execute_manual_import(files, import_mode="move")

    sent = json.loads(route.calls.last.request.content)
    assert sent == {"name": "ManualImport", "files": files, "importMode": "move"}
    assert result["id"] == 123


@respx.mock
async def test_command_generic():
    route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json=load_fixture("command_response.json"))
    )

    async with SonarrClient(BASE_URL, API_KEY) as client:
        await client.command("EpisodeSearch", episodeIds=[555, 556])

    sent = json.loads(route.calls.last.request.content)
    assert sent == {"name": "EpisodeSearch", "episodeIds": [555, 556]}


@respx.mock
async def test_retries_on_503_then_succeeds():
    route = respx.get(f"{API_URL}/system/status").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=load_fixture("system_status.json")),
        ]
    )
    async with SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0)) as client:
        status = await client.system_status()

    assert status.version == "3.0.10.1567"
    assert route.call_count == 2


@respx.mock
async def test_400_raises_without_retry():
    route = respx.get(f"{API_URL}/system/status").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    async with SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0)) as client:
        with pytest.raises(SonarrError) as exc_info:
            await client.system_status()

    assert exc_info.value.status_code == 400
    assert exc_info.value.body == "bad request"
    assert route.call_count == 1


@respx.mock
async def test_transport_error_is_retried():
    route = respx.get(f"{API_URL}/system/status").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=load_fixture("system_status.json")),
        ]
    )
    async with SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0)) as client:
        status = await client.system_status()

    assert status.version == "3.0.10.1567"
    assert route.call_count == 2


@respx.mock
async def test_exhausted_retries_on_persistent_5xx_raises_sonarr_error():
    route = respx.get(f"{API_URL}/system/status").mock(return_value=httpx.Response(503))
    async with SonarrClient(BASE_URL, API_KEY, max_retries=2, backoff=(0, 0)) as client:
        with pytest.raises(SonarrError) as exc_info:
            await client.system_status()

    assert exc_info.value.status_code == 503
    assert route.call_count == 3


@respx.mock
async def test_retry_backoff_index_clamped_when_max_retries_exceeds_backoff_length():
    # backoff has only 1 entry but max_retries=3 requires indexing attempts
    # 0..2; _request must clamp the index instead of raising IndexError.
    route = respx.get(f"{API_URL}/system/status").mock(return_value=httpx.Response(503))
    async with SonarrClient(BASE_URL, API_KEY, max_retries=3, backoff=(0,)) as client:
        with pytest.raises(SonarrError) as exc_info:
            await client.system_status()

    assert exc_info.value.status_code == 503
    assert route.call_count == 4


async def test_context_manager_closes_underlying_client():
    async with SonarrClient(BASE_URL, API_KEY) as client:
        inner = client._client
        assert inner.is_closed is False

    assert inner.is_closed is True


# -- dry_run ------------------------------------------------------------
#
# No mutating route is mocked in any of these: respx raises on an unmocked
# request, so a stray real HTTP call would fail the test just as surely as
# an explicit call-count assertion.


@respx.mock
async def test_delete_episode_file_dry_run_makes_no_call_and_logs(caplog):
    async with SonarrClient(BASE_URL, API_KEY, dry_run=True) as client:
        with caplog.at_level("WARNING"):
            result = await client.delete_episode_file(9001)

    assert result is None
    assert respx.calls.call_count == 0
    assert any("DRY-RUN" in record.message for record in caplog.records)


@respx.mock
async def test_mark_history_failed_dry_run_makes_no_call_and_logs(caplog):
    async with SonarrClient(BASE_URL, API_KEY, dry_run=True) as client:
        with caplog.at_level("WARNING"):
            result = await client.mark_history_failed(101)

    assert result is None
    assert respx.calls.call_count == 0
    assert any("DRY-RUN" in record.message for record in caplog.records)


@respx.mock
async def test_execute_manual_import_dry_run_makes_no_call_and_logs(caplog):
    files = [{"path": "/staging/x.mkv", "episodeIds": [555]}]
    async with SonarrClient(BASE_URL, API_KEY, dry_run=True) as client:
        with caplog.at_level("WARNING"):
            result = await client.execute_manual_import(files, import_mode="move")

    assert result == {"dryRun": True}
    assert respx.calls.call_count == 0
    assert any("DRY-RUN" in record.message for record in caplog.records)


@respx.mock
async def test_command_dry_run_makes_no_call_and_logs(caplog):
    async with SonarrClient(BASE_URL, API_KEY, dry_run=True) as client:
        with caplog.at_level("WARNING"):
            result = await client.command("EpisodeSearch", episodeIds=[555])

    assert result == {"dryRun": True}
    assert respx.calls.call_count == 0
    assert any("DRY-RUN" in record.message for record in caplog.records)


@respx.mock
async def test_get_methods_unaffected_by_dry_run():
    route = respx.get(f"{API_URL}/system/status").mock(
        return_value=httpx.Response(200, json=load_fixture("system_status.json"))
    )
    async with SonarrClient(BASE_URL, API_KEY, dry_run=True) as client:
        status = await client.system_status()

    assert status.version == "3.0.10.1567"
    assert route.called
