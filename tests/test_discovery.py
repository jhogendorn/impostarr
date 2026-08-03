from __future__ import annotations

import logging

import httpx
import pytest
import respx
from sqlalchemy.exc import IntegrityError

from impostarr.config import PathMapping, Settings, SonarrInstance
from impostarr.db import init_db, make_session_factory
from impostarr.discovery import Discoverer, hash_file
from impostarr.models import File, Instance, Job
from impostarr.sonarr import SonarrClient

BASE_URL = "http://sonarr.test:8989"
API_URL = f"{BASE_URL}/api/v3"
API_KEY = "test-api-key"

HISTORY_PARAMS = {
    "eventType": "3",
    "sortKey": "date",
    "sortDirection": "descending",
    "pageSize": "100",
}


# -- fixtures / helpers -------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    engine = init_db(settings)
    return make_session_factory(engine)


def make_instance_cfg(tmp_path, **overrides) -> SonarrInstance:
    defaults: dict = {
        "name": "main",
        "url": BASE_URL,
        "api_key": API_KEY,
        "path_mappings": [PathMapping(sonarr="/tv", local=str(tmp_path / "tv"))],
        "staging_dir": str(tmp_path / "staging"),
        "watch_dirs": [],
    }
    defaults.update(overrides)
    return SonarrInstance(**defaults)


def make_client() -> SonarrClient:
    return SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0))


def history_record(
    id_,
    *,
    episode_id=555,
    series_id=42,
    file_id=9001,
    source_title="Show.Name.S01E02.1080p.WEB-DL",
    download_id="dl-1",
    guid="guid-1",
    indexer="NZBgeek",
    date="2026-07-30T10:15:00Z",
):
    return {
        "id": id_,
        "episodeId": episode_id,
        "seriesId": series_id,
        "sourceTitle": source_title,
        "downloadId": download_id,
        "eventType": "downloadFolderImported",
        "date": date,
        "quality": {"quality": {"id": 7, "name": "WEBDL-1080p"}},
        "languages": [{"id": 1, "name": "English"}],
        "data": {"fileId": str(file_id), "guid": guid, "indexer": indexer},
    }


def history_page(records: list[dict]) -> dict:
    return {
        "page": 1,
        "pageSize": 100,
        "sortKey": "date",
        "sortDirection": "descending",
        "totalRecords": len(records),
        "records": records,
    }


EMPTY_HISTORY_PAGE = {
    "page": 1,
    "pageSize": 100,
    "sortKey": "date",
    "sortDirection": "descending",
    "totalRecords": 0,
    "records": [],
}


def episode_file_json(id_, *, series_id=42, path="/tv/Show/S01E02.mkv", size=100):
    return {
        "id": id_,
        "seriesId": series_id,
        "path": path,
        "size": size,
        "quality": {"quality": {"id": 7, "name": "WEBDL-1080p"}},
        "languages": [{"id": 1, "name": "English"}],
    }


def episode_json(id_, *, episode_file_id, season_number=1, episode_number=1, has_file=True):
    return {
        "id": id_,
        "seasonNumber": season_number,
        "episodeNumber": episode_number,
        "episodeFileId": episode_file_id,
        "hasFile": has_file,
    }


def mock_episodes(series_id: int, episodes: list[dict]) -> None:
    respx.get(f"{API_URL}/episode", params={"seriesId": str(series_id)}).mock(
        return_value=httpx.Response(200, json=episodes)
    )


def mock_history_once(records: list[dict]) -> None:
    """Mock a single poll: one page of `records` (descending order expected
    by the caller) followed by the trailing empty page the client always
    fetches when no record on the page is at/below the watermark."""
    respx.get(f"{API_URL}/history", params=HISTORY_PARAMS).mock(
        side_effect=[
            httpx.Response(200, json=history_page(records)),
            httpx.Response(200, json=EMPTY_HISTORY_PAGE),
        ]
    )


# -- hash_file ------------------------------------------------------------


def test_hash_file_deterministic_and_differs_when_content_differs(tmp_path):
    f1 = tmp_path / "a.bin"
    f1.write_bytes(b"hello world" * 1000)

    assert hash_file(f1) == hash_file(f1)

    f2 = tmp_path / "b.bin"
    f2.write_bytes(b"hello world" * 999 + b"different!!")

    assert hash_file(f2) != hash_file(f1)


def test_hash_file_only_samples_head_tail_and_size(tmp_path):
    # Larger than the 8MiB head/tail chunks so a middle-only change can't
    # touch either sampled region, but the file size stays identical.
    head = b"H" * (8 * 1024 * 1024)
    tail = b"T" * (8 * 1024 * 1024)

    same_middle_a = tmp_path / "middle_a.bin"
    same_middle_a.write_bytes(head + b"A" * 16 + tail)
    same_middle_b = tmp_path / "middle_b.bin"
    same_middle_b.write_bytes(head + b"B" * 16 + tail)

    # Only the middle differs (same size) -> hash unaffected.
    assert hash_file(same_middle_a) == hash_file(same_middle_b)

    different_tail = tmp_path / "different_tail.bin"
    different_tail.write_bytes(head + b"A" * 16 + (b"X" * 8 * 1024 * 1024))

    # Tail differs -> hash changes.
    assert hash_file(different_tail) != hash_file(same_middle_a)


# -- poll_once ------------------------------------------------------------


@respx.mock
async def test_poll_once_creates_job_and_dedupes_same_episode_file_on_second_poll(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    local_path = tmp_path / "tv" / "Show" / "S01E02.mkv"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"episode content")

    mock_history_once([history_record(101, file_id=9001)])
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/Show/S01E02.mkv"))
    )
    mock_episodes(42, [episode_json(555, episode_file_id=9001)])

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result_first = await discoverer.poll_once()

    assert result_first == (1, 0)
    with session_factory() as session:
        assert session.query(File).count() == 1
        instance = session.query(Instance).one()
        assert instance.history_watermark == 101

    # Second poll: a new, higher history id for a re-import event that
    # references the SAME episode_file_id (already captured) -> dedupe by
    # (instance_id, episode_file_id) must skip it, watermark still advances.
    respx.clear()
    mock_history_once([history_record(102, file_id=9001)])

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result_second = await discoverer.poll_once()

    assert result_second == (0, 0)
    with session_factory() as session:
        assert session.query(File).count() == 1
        instance = session.query(Instance).one()
        assert instance.history_watermark == 102


@respx.mock
async def test_poll_once_multi_episode_file_captures_all_episode_ids(tmp_path, session_factory):
    # Sonarr emits one history record per episode for a multi-episode
    # import, both sharing the same episodeFileId. The sibling record hits
    # the (instance_id, episode_file_id) dedupe and is dropped, but the
    # first-captured files row must carry BOTH episode ids (needed for
    # EpisodeSearch remediation later), not just the first record's own.
    cfg = make_instance_cfg(tmp_path)
    local_path = tmp_path / "tv" / "Show" / "S01E01E02.mkv"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"multi-episode content")

    mock_history_once(
        [
            history_record(101, episode_id=555, series_id=42, file_id=9001),
            history_record(102, episode_id=556, series_id=42, file_id=9001),
        ]
    )
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(
            200, json=episode_file_json(9001, path="/tv/Show/S01E01E02.mkv")
        )
    )
    mock_episodes(
        42,
        [
            episode_json(555, episode_file_id=9001),
            episode_json(556, episode_file_id=9001, episode_number=2),
        ],
    )

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.poll_once()

    assert result == (1, 0)
    with session_factory() as session:
        assert session.query(File).count() == 1
        file_row = session.query(File).one()
        assert file_row.episode_ids == [555, 556]
        assert session.query(Job).count() == 1


@respx.mock
async def test_poll_once_path_mapping_picks_longest_prefix(tmp_path, session_factory):
    cfg = make_instance_cfg(
        tmp_path,
        path_mappings=[
            PathMapping(sonarr="/tv", local=str(tmp_path / "tv1")),
            PathMapping(sonarr="/tv/Show", local=str(tmp_path / "tv2" / "ShowSpecial")),
        ],
    )
    sonarr_path = "/tv/Show/Season 01/ep.mkv"
    expected_local = tmp_path / "tv2" / "ShowSpecial" / "Season 01" / "ep.mkv"
    expected_local.parent.mkdir(parents=True)
    expected_local.write_bytes(b"content")

    mock_history_once([history_record(101, file_id=9001)])
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path=sonarr_path))
    )
    mock_episodes(42, [episode_json(555, episode_file_id=9001)])

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.poll_once()

    assert result == (1, 0)
    with session_factory() as session:
        file_row = session.query(File).one()
        assert file_row.local_path == str(expected_local)


@respx.mock
async def test_poll_once_watch_dirs_filter_excludes_out_of_tree_files(tmp_path, session_factory):
    cfg = make_instance_cfg(
        tmp_path,
        watch_dirs=[str(tmp_path / "tv" / "OtherShow")],
    )

    mock_history_once([history_record(101, file_id=9001)])
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/Show/S01E02.mkv"))
    )

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.poll_once()

    assert result == (0, 0)
    with session_factory() as session:
        assert session.query(File).count() == 0


@respx.mock
async def test_poll_once_unmapped_path_skipped_with_warning(tmp_path, session_factory, caplog):
    cfg = make_instance_cfg(tmp_path, path_mappings=[])

    mock_history_once([history_record(101, file_id=9001)])
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/Show/S01E02.mkv"))
    )

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        with caplog.at_level(logging.WARNING, logger="impostarr.discovery"):
            result = await discoverer.poll_once()

    assert result == (0, 0)
    with session_factory() as session:
        assert session.query(File).count() == 0
    assert any(
        "no path mapping" in record.message and "/tv/Show/S01E02.mkv" in record.message
        for record in caplog.records
    )


@respx.mock
async def test_poll_once_unreadable_file_skipped_batch_continues(tmp_path, session_factory, caplog):
    # Live evidence: a file with restrictive permissions (unreadable by the
    # service identity) must not crash the whole poll -- log a WARNING,
    # skip just that file, and keep processing the rest of the batch.
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv" / "Show"
    tv_dir.mkdir(parents=True)
    readable = tv_dir / "S01E02.mkv"
    readable.write_bytes(b"episode content")
    unreadable = tv_dir / "S01E03.mkv"
    unreadable.write_bytes(b"episode content")
    unreadable.chmod(0o000)

    try:
        mock_history_once(
            [history_record(101, episode_id=555, file_id=9001), history_record(102, episode_id=556, file_id=9002)]
        )
        respx.get(f"{API_URL}/episodefile/9001").mock(
            return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/Show/S01E02.mkv"))
        )
        respx.get(f"{API_URL}/episodefile/9002").mock(
            return_value=httpx.Response(200, json=episode_file_json(9002, path="/tv/Show/S01E03.mkv"))
        )
        mock_episodes(
            42,
            [
                episode_json(555, episode_file_id=9001),
                episode_json(556, episode_file_id=9002, episode_number=2),
            ],
        )

        async with make_client() as client:
            discoverer = Discoverer(cfg, client, session_factory)
            with caplog.at_level(logging.WARNING, logger="impostarr.discovery"):
                result = await discoverer.poll_once()

        assert result == (1, 1)
        with session_factory() as session:
            assert session.query(File).count() == 1
            instance = session.query(Instance).one()
            # Watermark still advances past the skipped record.
            assert instance.history_watermark == 102
        assert any(
            "cannot read local file" in record.message and "S01E03.mkv" in record.message
            for record in caplog.records
        )
    finally:
        unreadable.chmod(0o644)


@respx.mock
async def test_poll_once_watermark_advances_only_on_success(tmp_path, session_factory, monkeypatch):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    (tv_dir / "ok.mkv").write_bytes(b"good file content")
    (tv_dir / "bad.mkv").write_bytes(b"conflicting file content")

    # Bootstrap the instance row and pre-insert a "bad row" that will
    # collide (same instance_id + episode_file_id) with the second history
    # record once the dedupe check is bypassed below.
    with session_factory() as session:
        instance = Instance(name=cfg.name, url=cfg.url, history_watermark=0)
        session.add(instance)
        session.commit()
        session.add(
            File(
                instance_id=instance.id,
                sonarr_path="/tv/preexisting.mkv",
                local_path=str(tv_dir / "preexisting.mkv"),
                size=1,
                content_hash="preexisting-hash",
                series_id=42,
                episode_ids=[999],
                episode_file_id=9002,
                quality={},
                languages=[],
            )
        )
        session.commit()

    mock_history_once(
        [history_record(201, episode_id=555, file_id=9001), history_record(202, episode_id=556, file_id=9002)]
    )
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/ok.mkv"))
    )
    respx.get(f"{API_URL}/episodefile/9002").mock(
        return_value=httpx.Response(200, json=episode_file_json(9002, path="/tv/bad.mkv"))
    )
    mock_episodes(
        42,
        [
            episode_json(555, episode_file_id=9001),
            episode_json(556, episode_file_id=9002, episode_number=2),
        ],
    )

    # Bypass the dedupe check so processing reaches the DB insert for the
    # second (colliding) record instead of being cleanly skipped.
    monkeypatch.setattr(Discoverer, "_file_exists", lambda self, session, instance_id, episode_file_id: False)

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        with pytest.raises(IntegrityError):
            await discoverer.poll_once()

    with session_factory() as session:
        # Only the pre-existing bad row -> record 1's file was staged but
        # never committed, proving the whole batch rolled back together.
        assert session.query(File).count() == 1
        assert session.query(File).one().sonarr_path == "/tv/preexisting.mkv"
        instance = session.query(Instance).one()
        assert instance.history_watermark == 0


# -- sync timestamps -------------------------------------------------------


@respx.mock
async def test_poll_once_sets_last_polled_at_on_success(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    local_path = tmp_path / "tv" / "Show" / "S01E02.mkv"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"episode content")

    mock_history_once([history_record(101, file_id=9001)])
    respx.get(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json=episode_file_json(9001, path="/tv/Show/S01E02.mkv"))
    )
    mock_episodes(42, [episode_json(555, episode_file_id=9001)])

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        await discoverer.poll_once()

    with session_factory() as session:
        instance = session.query(Instance).one()
        assert instance.last_polled_at is not None
        assert instance.last_backfilled_at is None


@respx.mock
async def test_poll_once_sets_last_polled_at_even_when_no_new_records(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    mock_history_once([])

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.poll_once()

    assert result == (0, 0)
    with session_factory() as session:
        instance = session.query(Instance).one()
        assert instance.last_polled_at is not None


# -- backfill_step ----------------------------------------------------


@respx.mock
async def test_backfill_step_cursor_resumes_mid_series(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    for i in (301, 302, 303):
        (tv_dir / f"ep{i}.mkv").write_bytes(f"content-{i}".encode())

    respx.get(f"{API_URL}/series").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 42, "title": "Show Name", "tvdbId": 1, "imdbId": None, "tmdbId": None, "titleSlug": "show"}],
        )
    )
    respx.get(f"{API_URL}/episodefile", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[episode_file_json(fid, path=f"/tv/ep{fid}.mkv") for fid in (301, 302, 303)],
        )
    )
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 700 + i,
                    "seasonNumber": 1,
                    "episodeNumber": i,
                    "episodeFileId": fid,
                    "hasFile": True,
                }
                for i, fid in enumerate((301, 302, 303), start=1)
            ],
        )
    )

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result_first = await discoverer.backfill_step(batch_size=2)

    assert result_first == (2, 0)
    with session_factory() as session:
        assert {f.episode_file_id for f in session.query(File).all()} == {301, 302}
        instance = session.query(Instance).one()
        assert instance.backfill_cursor == {"series_id": 42, "episode_file_id": 302}

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result_second = await discoverer.backfill_step(batch_size=2)

    assert result_second == (1, 0)
    with session_factory() as session:
        assert {f.episode_file_id for f in session.query(File).all()} == {301, 302, 303}
        instance = session.query(Instance).one()
        # Only one series, now fully processed -> cursor resets to null.
        assert instance.backfill_cursor is None
        file_303 = session.query(File).filter_by(episode_file_id=303).one()
        assert file_303.episode_ids == [703]


@respx.mock
async def test_backfill_step_sets_last_backfilled_at(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    (tv_dir / "ep301.mkv").write_bytes(b"content-301")

    respx.get(f"{API_URL}/series").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 42, "title": "Show Name", "tvdbId": 1, "imdbId": None, "tmdbId": None, "titleSlug": "show"}],
        )
    )
    respx.get(f"{API_URL}/episodefile", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(200, json=[episode_file_json(301, path="/tv/ep301.mkv")])
    )
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 701, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 301, "hasFile": True}],
        )
    )

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        await discoverer.backfill_step(batch_size=10)

    with session_factory() as session:
        instance = session.query(Instance).one()
        assert instance.last_backfilled_at is not None


def _mock_two_series() -> None:
    """Series 10 (file 301) and series 20 (file 401), each with one
    episode file -- shared setup for the reset/series_id targeting tests."""
    respx.get(f"{API_URL}/series").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 10, "title": "Show A", "tvdbId": 1, "imdbId": None, "tmdbId": None, "titleSlug": "a"},
                {"id": 20, "title": "Show B", "tvdbId": 2, "imdbId": None, "tmdbId": None, "titleSlug": "b"},
            ],
        )
    )
    respx.get(f"{API_URL}/episodefile", params={"seriesId": "10"}).mock(
        return_value=httpx.Response(200, json=[episode_file_json(301, series_id=10, path="/tv/ep301.mkv")])
    )
    respx.get(f"{API_URL}/episode", params={"seriesId": "10"}).mock(
        return_value=httpx.Response(
            200, json=[{"id": 701, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 301, "hasFile": True}]
        )
    )
    respx.get(f"{API_URL}/episodefile", params={"seriesId": "20"}).mock(
        return_value=httpx.Response(200, json=[episode_file_json(401, series_id=20, path="/tv/ep401.mkv")])
    )
    respx.get(f"{API_URL}/episode", params={"seriesId": "20"}).mock(
        return_value=httpx.Response(
            200, json=[{"id": 801, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 401, "hasFile": True}]
        )
    )


@respx.mock
async def test_backfill_step_unreadable_file_skipped_batch_continues(tmp_path, session_factory, caplog):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    (tv_dir / "ep301.mkv").write_bytes(b"content-301")
    unreadable = tv_dir / "ep302.mkv"
    unreadable.write_bytes(b"content-302")
    unreadable.chmod(0o000)

    try:
        respx.get(f"{API_URL}/series").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": 42, "title": "Show Name", "tvdbId": 1, "imdbId": None, "tmdbId": None, "titleSlug": "show"}],
            )
        )
        respx.get(f"{API_URL}/episodefile", params={"seriesId": "42"}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    episode_file_json(301, path="/tv/ep301.mkv"),
                    episode_file_json(302, path="/tv/ep302.mkv"),
                ],
            )
        )
        respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 701, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 301, "hasFile": True},
                    {"id": 702, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 302, "hasFile": True},
                ],
            )
        )

        async with make_client() as client:
            discoverer = Discoverer(cfg, client, session_factory)
            with caplog.at_level(logging.WARNING, logger="impostarr.discovery"):
                result = await discoverer.backfill_step(batch_size=10)

        assert result == (1, 1)
        with session_factory() as session:
            assert {f.episode_file_id for f in session.query(File).all()} == {301}
        assert any(
            "cannot read local file" in record.message and "ep302.mkv" in record.message
            for record in caplog.records
        )
    finally:
        unreadable.chmod(0o644)


@respx.mock
async def test_backfill_step_reset_ignores_persisted_cursor(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    (tv_dir / "ep301.mkv").write_bytes(b"content-301")
    (tv_dir / "ep401.mkv").write_bytes(b"content-401")
    _mock_two_series()

    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL, backfill_cursor={"series_id": 20, "episode_file_id": None})
        session.add(instance)
        session.commit()

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.backfill_step(batch_size=10, reset=True)

    # reset=True starts from the very beginning, so series 10 (which the
    # persisted cursor would otherwise skip past) gets captured too.
    assert result == (2, 0)
    with session_factory() as session:
        assert {f.episode_file_id for f in session.query(File).all()} == {301, 401}
        instance = session.query(Instance).one()
        assert instance.backfill_cursor is None  # both series walked to completion


@respx.mock
async def test_backfill_step_series_id_skips_earlier_series(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    tv_dir = tmp_path / "tv"
    tv_dir.mkdir()
    (tv_dir / "ep301.mkv").write_bytes(b"content-301")
    (tv_dir / "ep401.mkv").write_bytes(b"content-401")
    _mock_two_series()

    async with make_client() as client:
        discoverer = Discoverer(cfg, client, session_factory)
        result = await discoverer.backfill_step(batch_size=10, series_id=20)

    # series_id=20 starts at series 20 directly -- series 10 is never
    # visited at all (not even dedupe-checked), regardless of there being
    # no persisted cursor.
    assert result == (1, 0)
    with session_factory() as session:
        assert {f.episode_file_id for f in session.query(File).all()} == {401}
