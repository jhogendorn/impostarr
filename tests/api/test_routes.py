from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from impostarr import jobs
from impostarr.config import (
    RefSubsConfig,
    Settings,
    SonarrInstance,
    ThrottleConfig,
    TrashConfig,
    WorkersConfig,
)
from impostarr.discovery import Discoverer, DiscoveryResult
from impostarr.jobs import claim_next
from impostarr.main import create_app
from impostarr.models import (
    Asset,
    File,
    FrameHash,
    Instance,
    Job,
    PhashCorpusEntry,
    TrashItem,
    Verdict,
)
from impostarr.models import PluginResult as PluginResultRow
from impostarr.refsubs import RefSubService

API_PREFIX = "/api/v1"
BASE_URL = "http://sonarr.test:8989"
API_URL = f"{BASE_URL}/api/v3"
API_KEY = "test-api-key"


# -- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def app_no_instance(tmp_path):
    return create_app(Settings(state_dir=tmp_path / "state"))


@pytest.fixture
def app(tmp_path, monkeypatch):
    # No real network from the discovery loop: poll_once is patched to a
    # no-op before create_app builds the Discoverer, and pool_size=0 keeps
    # the worker pool from racing our manually-created jobs.
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    settings = Settings(
        state_dir=tmp_path / "state",
        sonarr=[
            SonarrInstance(
                name="main",
                url=BASE_URL,
                api_key=API_KEY,
                staging_dir=str(tmp_path / "staging"),
            )
        ],
        workers=WorkersConfig(pool_size=0),
        # Settings.trash defaults to /trash (real, unwritable in tests) —
        # anything that reaches Remediator.replace needs a trash dir it can
        # actually write to.
        trash=TrashConfig(dir=tmp_path / "trash"),
    )
    return create_app(settings)


def _session_factory(app):
    return app.state.session_factory


def _make_instance(session_factory, *, name="main") -> int:
    with session_factory() as session:
        instance = Instance(name=name, url=BASE_URL)
        session.add(instance)
        session.commit()
        return instance.id


def _make_file(session_factory, instance_id: int, **overrides) -> int:
    defaults = {
        "instance_id": instance_id,
        "sonarr_path": "/tv/Show/S01E01.mkv",
        "local_path": "/media/tv/Show/S01E01.mkv",
        "size": 1,
        "content_hash": "hash1",
        "series_id": 42,
        "episode_ids": [101],
        "episode_file_id": 9001,
        "quality": {},
        "languages": [],
    }
    defaults.update(overrides)
    with session_factory() as session:
        file = File(**defaults)
        session.add(file)
        session.commit()
        return file.id


def _make_job(session_factory, file_id: int, status: str = "pending", **overrides) -> int:
    with session_factory() as session:
        job = Job(file_id=file_id, status=status, **overrides)
        session.add(job)
        session.commit()
        return job.id


def _make_verdict(session_factory, job_id: int, **overrides) -> int:
    defaults = {"outcome": "quarantine", "source": "auto"}
    defaults.update(overrides)
    with session_factory() as session:
        verdict = Verdict(job_id=job_id, **defaults)
        session.add(verdict)
        session.commit()
        return verdict.id


def _get_job(session_factory, job_id: int) -> Job:
    with session_factory() as session:
        return session.get(Job, job_id)


def _make_trash_item(session_factory, tmp_path, **overrides) -> int:
    trash_path = overrides.pop("trash_path", None)
    original_path = overrides.pop("original_path", None)
    if trash_path is None:
        trash_path = tmp_path / "trash" / "main" / "S01E01.mkv-1"
        trash_path.parent.mkdir(parents=True, exist_ok=True)
        trash_path.write_bytes(b"trashed content")
    if original_path is None:
        original_path = tmp_path / "media" / "Show" / "S01E01.mkv"
    defaults = {
        "instance": "main",
        "original_path": str(original_path),
        "trash_path": str(trash_path),
        "size": 16,
        "series_id": 42,
        "episode_ids": [101],
        "expires_at": datetime.now(UTC) + timedelta(days=14),
    }
    defaults.update(overrides)
    with session_factory() as session:
        item = TrashItem(**defaults)
        session.add(item)
        session.commit()
        return item.id


def _get_trash_item(session_factory, item_id: int) -> TrashItem:
    with session_factory() as session:
        return session.get(TrashItem, item_id)


def episodes_json(series_id=42):
    return [
        {
            "id": 101,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "episodeFileId": 9001,
            "hasFile": True,
        },
        {
            "id": 102,
            "seasonNumber": 1,
            "episodeNumber": 2,
            "episodeFileId": 0,
            "hasFile": False,
        },
    ]


def mock_episodes(series_id=42):
    respx.get(f"{API_URL}/episode", params={"seriesId": str(series_id)}).mock(
        return_value=httpx.Response(200, json=episodes_json(series_id))
    )


def series_json(series_id=42, **overrides):
    body = {
        "id": series_id,
        "title": "Test Show",
        "tvdbId": 12345,
        "imdbId": "tt1234567",
        "tmdbId": 6789,
        "titleSlug": "test-show",
    }
    body.update(overrides)
    return body


def mock_series(series_id=42, **overrides):
    respx.get(f"{API_URL}/series/{series_id}").mock(
        return_value=httpx.Response(200, json=series_json(series_id, **overrides))
    )


# -- healthz / status -----------------------------------------------------


def test_healthz(app_no_instance):
    with TestClient(app_no_instance) as client:
        response = client.get(f"{API_PREFIX}/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_shape(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["instances"] == [
        {
            "name": "main",
            "url": BASE_URL,
            "history_watermark": None,
            "backfill_cursor": None,
            "last_polled_at": None,
            "last_backfilled_at": None,
        }
    ]
    assert body["queues"]["quarantine"] == 1
    assert set(body["queues"]) == {
        "hold", "pending", "active", "matched", "quarantine", "inconclusive", "error", "remediated",
    }
    assert body["workers"] == {"pool_size": 0}


def test_status_summary_splits_unprocessed_and_processed(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    for status in ("hold", "pending", "active", "matched", "quarantine", "inconclusive", "error", "remediated"):
        file_id = _make_file(session_factory, instance_id, episode_file_id=hash(status) % 100000)
        _make_job(session_factory, file_id, status=status)

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")

    body = response.json()
    assert body["summary"] == {"unprocessed": 3, "processed": 5}


def test_status_system_fields_present(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")

    body = response.json()
    assert set(body["system"]) == {"cpu_percent", "mem_percent"}
    assert isinstance(body["system"]["cpu_percent"], (int, float))
    assert isinstance(body["system"]["mem_percent"], (int, float))


def test_status_approval_required_defaults_false(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["approval_required"] is False


def test_status_approval_required_reflects_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    settings = Settings(state_dir=tmp_path / "state", approval_required=True)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["approval_required"] is True


def test_status_active_jobs_shape(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")
    with session_factory() as session:
        claim_next(session, "worker-1")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")

    body = response.json()
    assert len(body["active_jobs"]) == 1
    entry = body["active_jobs"][0]
    assert entry["job_id"] == job_id
    assert entry["instance"] == "main"
    assert entry["series_id"] == 42
    assert entry["sonarr_path"] == "/tv/Show/S01E01.mkv"
    assert entry["claimed_by"] == "worker-1"
    assert entry["claimed_at"] is not None
    assert entry["elapsed_s"] >= 0


def test_create_app_with_empty_sonarr_boots_without_workers(app_no_instance):
    assert app_no_instance.state.pool_size == 0
    with TestClient(app_no_instance) as client:
        response = client.get(f"{API_PREFIX}/healthz")
    assert response.status_code == 200


def test_status_dry_run_defaults_false(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["dry_run"] is False


def test_status_dry_run_reflects_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    settings = Settings(state_dir=tmp_path / "state", dry_run=True)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["dry_run"] is True


def test_status_paused_defaults_false(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["paused"] is False


def test_status_paused_reflects_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    settings = Settings(state_dir=tmp_path / "state", throttle=ThrottleConfig(paused=True))
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["paused"] is True


def test_pause_endpoint_flips_runtime_flag_not_config(app):
    assert app.state.settings.throttle.paused is False
    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/pause")
        assert response.status_code == 200
        assert response.json() == {"paused": True}

        status = client.get(f"{API_PREFIX}/status")
        assert status.json()["paused"] is True

    assert app.state.settings.throttle.paused is True


def test_resume_endpoint_clears_pause(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", throttle=ThrottleConfig(paused=True))
    with TestClient(create_app(settings)) as client:
        response = client.post(f"{API_PREFIX}/resume")
        assert response.status_code == 200
        assert response.json() == {"paused": False}

        status = client.get(f"{API_PREFIX}/status")
        assert status.json()["paused"] is False


def test_status_refsubs_quota_defaults_present(app):
    # main.create_app defaults refsubs.cache_dir to <state_dir>/refsubs_cache
    # when unset, so quota tracking (and therefore `refsubs_quota`) is
    # always available out of the box, not just when explicitly configured.
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["refsubs_quota"] == {"used": 0, "limit": 20}


def test_status_refsubs_quota_reflects_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    cache_dir = tmp_path / "refsubs_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "quota.json").write_text(
        json.dumps({"date": datetime.now(UTC).date().isoformat(), "count": 4})
    )
    settings = Settings(
        state_dir=tmp_path / "state", refsubs=RefSubsConfig(cache_dir=str(cache_dir), daily_quota=20)
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.json()["refsubs_quota"] == {"used": 4, "limit": 20}


def test_status_trash_count_reflects_active_items_only(app, tmp_path):
    session_factory = _session_factory(app)
    _make_trash_item(session_factory, tmp_path)
    deleted_id = _make_trash_item(session_factory, tmp_path, trash_path=tmp_path / "trash/main/x-2")
    with session_factory() as session:
        item = session.get(TrashItem, deleted_id)
        item.deleted_at = datetime.now(UTC)
        item.outcome = "deleted"
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")

    assert response.json()["trash_count"] == 1


# -- logs -------------------------------------------------------------------


def test_logs_endpoint_returns_captured_records(app):
    with TestClient(app) as client:
        logging.getLogger("impostarr.somewhere").warning("DRY-RUN: would do a thing")
        response = client.get(f"{API_PREFIX}/logs")

    assert response.status_code == 200
    body = response.json()
    messages = [item["message"] for item in body["items"]]
    assert "DRY-RUN: would do a thing" in messages


def test_logs_endpoint_filters_by_level(app):
    with TestClient(app) as client:
        logger = logging.getLogger("impostarr.somewhere")
        logger.info("info line")
        logger.error("error line")
        response = client.get(f"{API_PREFIX}/logs", params={"level": "ERROR"})

    assert response.status_code == 200
    messages = [item["message"] for item in response.json()["items"]]
    assert "error line" in messages
    assert "info line" not in messages


def test_logs_endpoint_respects_limit(app):
    with TestClient(app) as client:
        logger = logging.getLogger("impostarr.somewhere")
        for i in range(5):
            logger.info("msg-%d", i)
        response = client.get(f"{API_PREFIX}/logs", params={"limit": 2})

    items = response.json()["items"]
    assert len(items) == 2
    assert items[-1]["message"] == "msg-4"


def test_create_app_wires_shared_refsubservice_into_every_instance_deps(tmp_path, monkeypatch):
    # Regression: create_app used to pass refsubs=None into every
    # PipelineDeps, so whisper-subs' ctx.refsubs.get(...) call always
    # failed (caught internally -> status "error"), permanently dead.
    # No respx mocking needed: RefSubService/httpx.AsyncClient construction
    # makes no network calls.
    monkeypatch.setattr(Discoverer, "poll_once", AsyncMock(return_value=0))
    settings = Settings(
        state_dir=tmp_path / "state",
        sonarr=[
            SonarrInstance(
                name="one", url=BASE_URL, api_key=API_KEY, staging_dir=str(tmp_path / "staging1")
            ),
            SonarrInstance(
                name="two", url=BASE_URL, api_key=API_KEY, staging_dir=str(tmp_path / "staging2")
            ),
        ],
        workers=WorkersConfig(pool_size=0),
    )
    app = create_app(settings)

    deps_per_instance = app.state.deps_per_instance
    assert set(deps_per_instance) == {"one", "two"}
    refsubs_one = deps_per_instance["one"].refsubs
    refsubs_two = deps_per_instance["two"].refsubs
    assert refsubs_one is not None
    assert isinstance(refsubs_one, RefSubService)
    assert refsubs_one is refsubs_two  # instance-agnostic service, shared


def test_static_mount_absent_api_still_up(tmp_path, monkeypatch):
    # _resolve_web_dist anchors to the repo root (see main.py), so this
    # can't be forced to "absent" via chdir isolation alone — a local
    # `npm run build` leaving web/dist on disk would make that flaky.
    # Monkeypatch the resolver directly, before create_app() runs (the
    # app_no_instance fixture builds too early for that), to a path that
    # genuinely doesn't exist.
    monkeypatch.setattr("impostarr.main._resolve_web_dist", lambda: tmp_path / "nonexistent-dist")
    app = create_app(Settings(state_dir=tmp_path / "state"))

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/healthz")
        root_response = client.get("/")
    assert response.status_code == 200
    assert root_response.status_code == 404


def test_static_mount_present_serves_index(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>hello from web/dist</body></html>")
    monkeypatch.setattr("impostarr.main._resolve_web_dist", lambda: dist_dir)
    app = create_app(Settings(state_dir=tmp_path / "state"))

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "hello from web/dist" in response.text


def test_static_mount_cache_headers(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html><body>spa</body></html>")
    (dist_dir / "assets" / "index-abc123.js").write_text("console.log('x')")
    monkeypatch.setattr("impostarr.main._resolve_web_dist", lambda: dist_dir)
    app = create_app(Settings(state_dir=tmp_path / "state"))

    with TestClient(app) as client:
        index_response = client.get("/")
        asset_response = client.get("/assets/index-abc123.js")

    assert index_response.headers["cache-control"] == "no-cache"
    assert asset_response.headers["cache-control"] == "public, max-age=31536000, immutable"


# -- queues -----------------------------------------------------------------


def test_queues_paging_and_ordering(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    ids = []
    for i in range(3):
        file_id = _make_file(session_factory, instance_id, episode_file_id=9000 + i)
        job_id = _make_job(session_factory, file_id, status="quarantine")
        with session_factory() as session:
            job = session.get(Job, job_id)
            job.updated_at = datetime(2026, 1, 1, tzinfo=UTC).replace(hour=i)
            session.commit()
        ids.append(job_id)

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/queues/quarantine", params={"page_size": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    # Ordered updated_at desc: last-created job (hour=2) first.
    assert body["items"][0]["job_id"] == ids[2]
    assert body["items"][1]["job_id"] == ids[1]
    assert body["items"][0]["file"]["series_id"] == 42


def test_queues_invalid_status_400(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/queues/not-a-status")
    assert response.status_code == 400


def test_queues_echoes_page_size(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/queues/quarantine", params={"page_size": 5})

    assert response.json()["page_size"] == 5


def test_queues_includes_instance_name(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/queues/quarantine")

    assert response.json()["items"][0]["instance"] == "main"


def test_queues_instance_filter(app):
    session_factory = _session_factory(app)
    instance_a = _make_instance(session_factory, name="a")
    instance_b = _make_instance(session_factory, name="b")
    file_a = _make_file(session_factory, instance_a, episode_file_id=1)
    file_b = _make_file(session_factory, instance_b, episode_file_id=2)
    job_a = _make_job(session_factory, file_a, status="quarantine")
    _make_job(session_factory, file_b, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/queues/quarantine", params={"instance": "a"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job_id"] == job_a
    assert body["items"][0]["instance"] == "a"


def test_queues_sort_created_at_ascending(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    ids = []
    for i in range(3):
        file_id = _make_file(session_factory, instance_id, episode_file_id=9100 + i)
        job_id = _make_job(session_factory, file_id, status="quarantine")
        with session_factory() as session:
            job = session.get(Job, job_id)
            job.created_at = datetime(2026, 1, 1, tzinfo=UTC).replace(hour=i)
            session.commit()
        ids.append(job_id)

    with TestClient(app) as client:
        response = client.get(
            f"{API_PREFIX}/queues/quarantine", params={"sort": "created_at", "dir": "asc"}
        )

    body = response.json()
    assert [item["job_id"] for item in body["items"]] == ids


def test_queues_sort_confidence_desc_nulls_last(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_low = _make_file(session_factory, instance_id, episode_file_id=9200)
    file_high = _make_file(session_factory, instance_id, episode_file_id=9201)
    file_none = _make_file(session_factory, instance_id, episode_file_id=9202)
    job_low = _make_job(session_factory, file_low, status="quarantine")
    job_high = _make_job(session_factory, file_high, status="quarantine")
    job_none = _make_job(session_factory, file_none, status="quarantine")
    _make_verdict(session_factory, job_low, s_claimed=0.2)
    _make_verdict(session_factory, job_high, s_claimed=0.9)
    # job_none gets no verdict row at all -> s_claimed is null via the
    # correlated subquery, must sort after every scored job regardless of dir.

    with TestClient(app) as client:
        desc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "confidence", "dir": "desc"})
        asc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "confidence", "dir": "asc"})

    assert [item["job_id"] for item in desc.json()["items"]] == [job_high, job_low, job_none]
    assert [item["job_id"] for item in asc.json()["items"]] == [job_low, job_high, job_none]


def test_queues_sort_series(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_a = _make_file(session_factory, instance_id, episode_file_id=9300, series_id=10)
    file_b = _make_file(session_factory, instance_id, episode_file_id=9301, series_id=30)
    file_c = _make_file(session_factory, instance_id, episode_file_id=9302, series_id=20)
    job_a = _make_job(session_factory, file_a, status="quarantine")
    job_b = _make_job(session_factory, file_b, status="quarantine")
    job_c = _make_job(session_factory, file_c, status="quarantine")

    with TestClient(app) as client:
        asc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "series", "dir": "asc"})
        desc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "series", "dir": "desc"})

    assert [item["job_id"] for item in asc.json()["items"]] == [job_a, job_c, job_b]
    assert [item["job_id"] for item in desc.json()["items"]] == [job_b, job_c, job_a]


def test_queues_sort_instance(app):
    session_factory = _session_factory(app)
    instance_a = _make_instance(session_factory, name="alpha")
    instance_b = _make_instance(session_factory, name="beta")
    file_a = _make_file(session_factory, instance_a, episode_file_id=9400)
    file_b = _make_file(session_factory, instance_b, episode_file_id=9401)
    job_a = _make_job(session_factory, file_a, status="quarantine")
    job_b = _make_job(session_factory, file_b, status="quarantine")

    with TestClient(app) as client:
        asc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "instance", "dir": "asc"})
        desc = client.get(f"{API_PREFIX}/queues/quarantine", params={"sort": "instance", "dir": "desc"})

    assert [item["job_id"] for item in asc.json()["items"]] == [job_a, job_b]
    assert [item["job_id"] for item in desc.json()["items"]] == [job_b, job_a]


def test_queues_order_is_deterministic_under_ties(app):
    """P0 regression. Root cause of "clicked E20's row, opened E01's job":
    GET /queues/{status}'s ORDER BY had no secondary/tiebreak key, so jobs
    tied on the sort column (very possible on updated_at/created_at/
    confidence/series/instance) came back in whatever order the DB engine's
    query plan happened to visit them — an implementation detail, not a
    guarantee, that a later *identical* request (e.g. App.tsx's debounced
    refetch after an unrelated SSE `job_update`) is not promised to
    reproduce. When the tied group's order silently flips between the
    user's initial render and a refetch, a row's on-screen position points
    at a different job than a moment before, with no visible reorder to
    warn the user before they click. Fix: every sort field now gets a
    deterministic `Job.id` tiebreak, so identical query params always
    return identical ordering regardless of unrelated writes elsewhere."""
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    tied_at = datetime(2026, 1, 1, tzinfo=UTC)
    ids = []
    for i in range(4):
        file_id = _make_file(session_factory, instance_id, episode_file_id=9500 + i)
        job_id = _make_job(session_factory, file_id, status="quarantine")
        with session_factory() as session:
            job = session.get(Job, job_id)
            job.updated_at = tied_at  # exact tie across all 4 jobs
            session.commit()
        ids.append(job_id)

    with TestClient(app) as client:
        first = client.get(f"{API_PREFIX}/queues/quarantine")
        # An unrelated write lands between the initial render and a
        # debounced SSE-triggered refetch of the identical query.
        other_file = _make_file(session_factory, instance_id, episode_file_id=9599)
        _make_job(session_factory, other_file, status="pending")
        second = client.get(f"{API_PREFIX}/queues/quarantine")

    first_ids = [item["job_id"] for item in first.json()["items"]]
    second_ids = [item["job_id"] for item in second.json()["items"]]
    # dir defaults to desc -> tiebreak is Job.id desc among the tied group.
    assert first_ids == second_ids == list(reversed(ids))


def test_click_after_sse_refetch_still_opens_correct_job(app):
    """P0 regression, literal reproduction of the reported bug: sort the
    queue, note which job a given row holds, simulate the debounced
    SSE-triggered refetch that follows an unrelated job update, then
    "click" that same row again (re-fetch the queue, read the row's
    job_id, then GET that job's detail exactly as InspectModal does) — it
    must resolve to the SAME job every time, never one that silently slid
    into that position because of an unstable tiebreak."""
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    tied_at = datetime(2026, 1, 1, tzinfo=UTC)
    e01_file = _make_file(session_factory, instance_id, episode_file_id=9601, episode_ids=[1])
    e20_file = _make_file(session_factory, instance_id, episode_file_id=9620, episode_ids=[20])
    e01_job = _make_job(session_factory, e01_file, status="quarantine")
    e20_job = _make_job(session_factory, e20_file, status="quarantine")
    for job_id in (e01_job, e20_job):
        with session_factory() as session:
            job = session.get(Job, job_id)
            job.updated_at = tied_at
            session.commit()

    with TestClient(app) as client:
        initial = client.get(f"{API_PREFIX}/queues/quarantine").json()
        # The row the user is about to click: whichever position e20_job holds.
        row_index = next(i for i, item in enumerate(initial["items"]) if item["job_id"] == e20_job)

        # Simulate an SSE `job_update` for a different, unrelated job,
        # triggering App.tsx's debounced refetch of this exact same query.
        other_file = _make_file(session_factory, instance_id, episode_file_id=9699)
        _make_job(session_factory, other_file, status="pending")
        refetched = client.get(f"{API_PREFIX}/queues/quarantine").json()

        clicked_job_id = refetched["items"][row_index]["job_id"]
        assert clicked_job_id == e20_job, "same row must still hold E20's job after the SSE-triggered refetch"

        modal = client.get(f"{API_PREFIX}/jobs/{clicked_job_id}").json()

    assert modal["job"]["id"] == e20_job
    assert modal["file"]["episode_ids"] == [20]
    assert e01_job != e20_job


# -- job detail / assets -----------------------------------------------------


@respx.mock
def test_job_detail_includes_plugin_results_and_verdict(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    with session_factory() as session:
        session.add(
            PluginResultRow(
                job_id=job_id,
                plugin_name="whisper-subs",
                plugin_version="1.0",
                status="ok",
                candidates=[],
                normalized=[{"kind": "in_series", "episode_ids": [101]}],
                input_fingerprint="fp1",
            )
        )
        session.commit()
    _make_verdict(session_factory, job_id, s_claimed=0.5, outcome="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["id"] == job_id
    assert body["instance"] == "main"
    assert body["file"]["series_id"] == 42
    assert len(body["plugin_results"]) == 1
    assert body["plugin_results"][0]["normalized"] == [{"kind": "in_series", "episode_ids": [101]}]
    assert body["verdict"]["outcome"] == "quarantine"
    assert body["verdict"]["dupe_info"] is None
    assert body["frame_hash_present"] is False


@respx.mock
def test_job_detail_includes_external_ids_on_successful_series_lookup(app):
    mock_series(title="Breaking Bad", tvdbId=81189, imdbId="tt0903747", tmdbId=1396)
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["external_ids"] == {
        "title": "Breaking Bad",
        "tvdb_id": 81189,
        "imdb_id": "tt0903747",
        "tmdb_id": 1396,
        "sonarr_url": f"{BASE_URL}/series/test-show",
    }


@respx.mock
def test_job_detail_external_ids_null_on_series_lookup_failure(app):
    respx.get(f"{API_URL}/series/42").mock(return_value=httpx.Response(404, json={}))
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["external_ids"] is None


def test_job_detail_external_ids_null_when_no_runtime_configured(app_no_instance):
    session_factory = _session_factory(app_no_instance)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app_no_instance) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["external_ids"] is None


@respx.mock
def test_job_detail_includes_dupe_info(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    dupe_info = {"duplicate_of_file_id": 999, "similarity": 0.95, "sonarr_path": "/tv/Other/S01E01.mkv"}
    _make_verdict(session_factory, job_id, outcome="quarantine", dupe_info=dupe_info)

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.json()["verdict"]["dupe_info"] == dupe_info


@respx.mock
def test_job_detail_verdict_apply_at_defaults_null(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id, outcome="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.json()["verdict"]["apply_at"] is None


@respx.mock
def test_job_detail_verdict_apply_at_round_trips(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    apply_at = "2026-08-04T00:00:00+00:00"
    _make_verdict(session_factory, job_id, outcome="quarantine", apply_at=apply_at)

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.json()["verdict"]["apply_at"] == apply_at


@respx.mock
def test_job_detail_includes_frame_hash_and_phash_corpus(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    with session_factory() as session:
        frame_hash = FrameHash(
            file_id=file_id, algo="phash", version=1, timestamps=[0.5, 1.5], hashes=["a", "b"]
        )
        session.add(frame_hash)
        session.commit()
        session.add(
            PhashCorpusEntry(
                frame_hash_id=frame_hash.id,
                external_ids={},
                season=1,
                episodes=[1],
                confidence=0.97,
                source="auto",
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    body = response.json()
    assert body["frame_hash"] == {"algo": "phash", "version": 1, "n_frames": 2}
    assert body["phash_corpus"] == {"confidence": 0.97, "source": "auto"}


@respx.mock
def test_job_detail_frame_hash_and_phash_corpus_null_when_absent(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    body = response.json()
    assert body["frame_hash"] is None
    assert body["phash_corpus"] is None


def test_job_detail_404_unknown(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/999999")
    assert response.status_code == 404


# -- episode_labels (P0.5) ----------------------------------------------


def _episodes_with_titles(series_id=42):
    respx.get(f"{API_URL}/episode", params={"seriesId": str(series_id)}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 101, "title": "Pilot", "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 9001, "hasFile": True},
                {"id": 102, "title": "Second", "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 0, "hasFile": False},
                {"id": 103, "title": "Third", "seasonNumber": 1, "episodeNumber": 3, "episodeFileId": 0, "hasFile": False},
            ],
        )
    )


@respx.mock
def test_job_detail_episode_labels_resolves_file_proposed_and_normalized_ids(app):
    mock_series()
    _episodes_with_titles()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id, episode_ids=[101])
    job_id = _make_job(session_factory, file_id, status="quarantine")
    with session_factory() as session:
        session.add(
            PluginResultRow(
                job_id=job_id,
                plugin_name="whisper-subs",
                plugin_version="1.0",
                status="ok",
                candidates=[],
                normalized=[{"kind": "in_series", "episode_ids": [103]}],
                input_fingerprint="fp1",
            )
        )
        session.commit()
    _make_verdict(
        session_factory, job_id, s_claimed=0.1, s_alt=0.9, outcome="quarantine",
        proposed_action={"kind": "remap", "target_episode_ids": [102]},
    )

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    labels = response.json()["episode_labels"]
    # file's own claimed episode, the proposed remap target, and the
    # plugin's in-series candidate all resolve — not just the claimed one.
    assert labels["101"] == {"id": 101, "season": 1, "episode": 1, "title": "Pilot"}
    assert labels["102"] == {"id": 102, "season": 1, "episode": 2, "title": "Second"}
    assert labels["103"] == {"id": 103, "season": 1, "episode": 3, "title": "Third"}


@respx.mock
def test_job_detail_episode_labels_empty_on_lookup_failure(app):
    mock_series()
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(return_value=httpx.Response(500, json={}))
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["episode_labels"] == {}


# -- series_episodes (inspect v3 episode picker) -------------------------


@respx.mock
def test_job_detail_series_episodes_includes_all_and_sorted(app):
    mock_series()
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 201, "title": "S2E1", "seasonNumber": 2, "episodeNumber": 1, "episodeFileId": 0, "hasFile": False},
                {"id": 101, "title": "Pilot", "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 9001, "hasFile": True},
                {"id": 102, "title": "Second", "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 0, "hasFile": False},
            ],
        )
    )
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id, episode_ids=[101])
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    body = response.json()
    # episode_labels stays filtered to referenced ids (just the file's own
    # claimed episode here)...
    assert set(body["episode_labels"].keys()) == {"101"}
    # ...but series_episodes lists every episode in the series, sorted by
    # (season, episode) ascending regardless of Sonarr's response order.
    assert body["series_episodes"] == [
        {"id": 101, "season": 1, "episode": 1, "title": "Pilot"},
        {"id": 102, "season": 1, "episode": 2, "title": "Second"},
        {"id": 201, "season": 2, "episode": 1, "title": "S2E1"},
    ]


@respx.mock
def test_job_detail_series_episodes_empty_on_lookup_failure(app):
    mock_series()
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(return_value=httpx.Response(500, json={}))
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["series_episodes"] == []


# -- embedded subs / reference subtitles (three-way text comparison) -----


@respx.mock
def test_job_detail_embedded_subs_payload_parses_srt_from_disk(app, tmp_path):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    srt_path = tmp_path / "embedded.en.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello embedded\n", encoding="utf-8")
    with session_factory() as session:
        session.add(
            Asset(
                file_id=file_id, type="subs", path=str(srt_path),
                tool_meta={"language": "en", "codec_name": "subrip"}, input_fingerprint="fp",
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    subs_asset = next(a for a in response.json()["assets"] if a["type"] == "subs")
    assert subs_asset["payload"] == {
        "cues": [{"start_s": 0.0, "text": "Hello embedded"}],
        "language": "en",
    }


@respx.mock
def test_job_detail_embedded_subs_payload_null_for_image_sub_codec(app, tmp_path):
    """PGS/VobSub subs have no text to parse — payload stays null rather
    than erroring on binary content."""
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    sup_path = tmp_path / "embedded.sup"
    sup_path.write_bytes(b"\x00\x01binary")
    with session_factory() as session:
        session.add(
            Asset(file_id=file_id, type="subs", path=str(sup_path), tool_meta={}, input_fingerprint="fp")
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    subs_asset = next(a for a in response.json()["assets"] if a["type"] == "subs")
    assert subs_asset["payload"] is None


@respx.mock
def test_job_detail_reference_subtitles_from_whisper_subs_evidence(app, tmp_path):
    mock_series()
    _episodes_with_titles()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    ref_path = tmp_path / "ref.S01E01.srt"
    ref_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello reference\n", encoding="utf-8")
    with session_factory() as session:
        session.add(
            PluginResultRow(
                job_id=job_id,
                plugin_name="whisper-subs",
                plugin_version="1.0",
                status="ok",
                candidates=[
                    {
                        "confidence": 0.9,
                        "ident": {"series": "claimed", "season": 1, "episodes": [1]},
                        "numbering": "tvdb",
                        "evidence": {"refsub_path": str(ref_path), "refsub_language": "en"},
                    }
                ],
                normalized=[{"kind": "in_series", "episode_ids": [101]}],
                input_fingerprint="fp1",
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    tracks = response.json()["reference_subtitles"]
    assert tracks == [
        {
            "label": "S01E01",
            "language": "en",
            "cues": [{"start_s": 0.0, "text": "Hello reference"}],
            "episode_ids": [101],
        }
    ]


@respx.mock
def test_job_detail_reference_subtitles_empty_when_no_whisper_subs_evidence(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.json()["reference_subtitles"] == []


# -- datapack -------------------------------------------------------------


@respx.mock
def test_job_datapack_is_attachment_with_full_bundle(app):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id, s_claimed=0.2, outcome="quarantine")
    _make_verdict(session_factory, job_id, s_claimed=0.9, outcome="matched")  # 2nd verdict = history

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}/datapack")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="impostarr-job-{job_id}-datapack.json"'
    body = response.json()
    assert body["job"]["id"] == job_id
    assert body["file"]["sonarr_path"] == "/tv/Show/S01E01.mkv"  # paths are NOT redacted
    assert [v["outcome"] for v in body["verdicts"]] == ["quarantine", "matched"]  # full history, not just latest
    assert body["app_version"]


def test_job_datapack_404_unknown(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/999999/datapack")
    assert response.status_code == 404


@respx.mock
def test_job_datapack_log_excerpt_scoped_to_job_timeframe(app, caplog):
    mock_series()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.created_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        job.updated_at = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        session.commit()

    buffer = app.state.log_buffer
    buffer._buffer.append({"ts": "2025-12-31T00:00:00+00:00", "level": "INFO", "logger": "x", "message": "before window", "exc": None})
    buffer._buffer.append({"ts": "2026-01-01T00:02:00+00:00", "level": "INFO", "logger": "x", "message": "inside window", "exc": None})
    buffer._buffer.append({"ts": "2026-02-01T00:00:00+00:00", "level": "INFO", "logger": "x", "message": "after window", "exc": None})

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}/datapack")

    messages = [r["message"] for r in response.json()["log_excerpt"]]
    assert messages == ["inside window"]


def test_job_asset_json_payload(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id)
    with session_factory() as session:
        asset = Asset(
            file_id=file_id, type="transcript", path=None,
            payload={"segments": [], "language": "en"}, input_fingerprint="fp",
        )
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}/assets/{asset_id}")

    assert response.status_code == 200
    assert response.json() == {"segments": [], "language": "en"}


def test_job_asset_file_response(app, tmp_path):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id)
    thumb = tmp_path / "frame0.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    with session_factory() as session:
        asset = Asset(
            file_id=file_id, type="frames", path=str(thumb), input_fingerprint="fp",
        )
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}/assets/{asset_id}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8\xff\xe0fakejpeg"


def test_job_asset_404_not_belonging(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id_a = _make_file(session_factory, instance_id, episode_file_id=1)
    file_id_b = _make_file(session_factory, instance_id, episode_file_id=2)
    job_a = _make_job(session_factory, file_id_a)
    with session_factory() as session:
        asset = Asset(file_id=file_id_b, type="probe", payload={"x": 1}, input_fingerprint="fp")
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_a}/assets/{asset_id}")

    assert response.status_code == 404


# -- verdict ------------------------------------------------------------


@respx.mock
def test_verdict_is_claimed_matches_and_writes_phash_corpus(app):
    mock_episodes()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id, episode_ids=[101])
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id, outcome="quarantine")
    with session_factory() as session:
        session.add(
            FrameHash(file_id=file_id, algo="phash", version=1, timestamps=[0.5], hashes=["abc"])
        )
        session.commit()

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "is_claimed"})

    assert response.status_code == 200
    body = response.json()
    assert body["job_status"] == "matched"

    assert _get_job(session_factory, job_id).status == "matched"
    with session_factory() as session:
        human_verdicts = session.execute(
            select(Verdict).where(Verdict.job_id == job_id, Verdict.source == "human")
        ).scalars().all()
        assert len(human_verdicts) == 1
        assert human_verdicts[0].outcome == "matched"
        assert human_verdicts[0].s_claimed == 1.0

        corpus_entries = session.execute(select(PhashCorpusEntry)).scalars().all()
        assert len(corpus_entries) == 1
        assert corpus_entries[0].confidence == 1.0
        assert corpus_entries[0].episodes == [1]
        assert corpus_entries[0].source == "human"


def test_verdict_is_claimed_with_no_frame_hash_writes_no_corpus_entry(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id, episode_ids=[101])
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id, outcome="quarantine")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "is_claimed"})

    assert response.status_code == 200
    assert response.json()["job_status"] == "matched"
    with session_factory() as session:
        corpus_entries = session.execute(select(PhashCorpusEntry)).scalars().all()
        assert corpus_entries == []


def test_verdict_on_active_job_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")
    with session_factory() as session:
        job = session.get(Job, job_id)
        claim_next(session, "worker-1")
        assert job.status == "active"

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "ignore"})

    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "is_claimed"},
        {"verdict": "is_other", "ident": {"season": 1, "episodes": [2]}},
        {"verdict": "ignore"},
    ],
    ids=["is_claimed", "is_other", "ignore"],
)
def test_verdict_on_hold_job_409(app, body):
    # Regression: the 409 gate must allowlist {quarantine, inconclusive},
    # not blocklist {active, pending} — a blocklist lets `hold` jobs
    # through, where is_claimed would leave the job stuck in `hold` with an
    # orphaned matched-verdict, and is_other/ignore would force
    # hold -> quarantine/inconclusive via the unfenced direct status write,
    # bypassing jobs.py's transition table entirely.
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="hold")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json=body)

    assert response.status_code == 409
    assert _get_job(session_factory, job_id).status == "hold"
    with session_factory() as session:
        verdicts = session.execute(select(Verdict).where(Verdict.job_id == job_id)).scalars().all()
        assert verdicts == []


def test_verdict_ignore_sets_inconclusive(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "ignore"})

    assert response.status_code == 200
    assert response.json()["job_status"] == "inconclusive"
    assert _get_job(session_factory, job_id).status == "inconclusive"


@respx.mock
def test_verdict_is_other_returns_proposed_remap(app):
    mock_episodes()
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id, episode_ids=[101])
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id)

    with TestClient(app) as client:
        response = client.post(
            f"{API_PREFIX}/jobs/{job_id}/verdict",
            json={"verdict": "is_other", "ident": {"season": 1, "episodes": [2]}},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["proposed_remap"] == {"kind": "remap", "target_episode_ids": [102]}
    assert _get_job(session_factory, job_id).status == "quarantine"


def test_verdict_double_submission_second_409s_single_verdict_row(app):
    # End-to-end regression for the fenced status-transition-before-verdict-
    # write ordering: a double submission against the same job (e.g. a
    # double-click, or a genuine race — exercised directly against
    # jobs.set_status_checked in tests/test_jobs.py) must leave exactly the
    # first submission's verdict row, not two. is_claimed (target "matched",
    # outside VERDICT_ALLOWED_STATUSES) rather than ignore/is_other: those
    # target quarantine/inconclusive, which are self-loop-valid transitions
    # (needed so is_other/ignore work starting from either allowed status),
    # so a second identical submission would legitimately succeed again
    # rather than 409 — not the scenario under test here.
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")

    with TestClient(app) as client:
        first = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "is_claimed"})
        second = client.post(f"{API_PREFIX}/jobs/{job_id}/verdict", json={"verdict": "is_claimed"})

    assert first.status_code == 200
    assert second.status_code == 409
    with session_factory() as session:
        verdicts = session.execute(select(Verdict).where(Verdict.job_id == job_id)).scalars().all()
        assert len(verdicts) == 1


# -- approve / reject -----------------------------------------------------


class FakeRemediator:
    calls: ClassVar[list[tuple]] = []

    def __init__(self, client, cfg, session_factory, dry_run: bool = False, trash_cfg=None) -> None:
        self.session_factory = session_factory

    async def replace(self, job, worker_id) -> None:
        FakeRemediator.calls.append(("replace", job.id, worker_id))
        with self.session_factory() as session:
            db_job = session.get(Job, job.id)
            jobs.release(session, db_job, "remediated", worker_id)

    async def remap(self, job, target_ids, worker_id) -> None:
        FakeRemediator.calls.append(("remap", job.id, worker_id))
        with self.session_factory() as session:
            db_job = session.get(Job, job.id)
            jobs.release(session, db_job, "remediated", worker_id)


def test_approve_quarantine_with_proposed_replace_invokes_remediator(app, monkeypatch):
    FakeRemediator.calls = []
    monkeypatch.setattr("impostarr.api.routes.Remediator", FakeRemediator)
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    _make_verdict(session_factory, job_id, proposed_action={"kind": "replace"})

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/approve")

    assert response.status_code == 200
    assert response.json() == {"result": "remediated"}
    assert FakeRemediator.calls == [("replace", job_id, "api-anon")]
    assert _get_job(session_factory, job_id).status == "remediated"


def test_approve_wrong_state_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/approve")

    assert response.status_code == 409


def test_replace_quarantine_with_no_proposed_action_invokes_remediator(app, monkeypatch):
    FakeRemediator.calls = []
    monkeypatch.setattr("impostarr.api.routes.Remediator", FakeRemediator)
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    # No verdict at all -- replace must not require one.

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/replace")

    assert response.status_code == 200
    assert response.json() == {"result": "remediated"}
    assert FakeRemediator.calls == [("replace", job_id, "api-anon")]
    assert _get_job(session_factory, job_id).status == "remediated"


def test_replace_inconclusive_invokes_remediator(app, monkeypatch):
    FakeRemediator.calls = []
    monkeypatch.setattr("impostarr.api.routes.Remediator", FakeRemediator)
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="inconclusive")
    _make_verdict(session_factory, job_id, outcome="inconclusive", proposed_action={"kind": "remap", "target_episode_ids": [1]})

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/replace")

    assert response.status_code == 200
    assert response.json() == {"result": "remediated"}
    assert FakeRemediator.calls == [("replace", job_id, "api-anon")]


def test_replace_wrong_state_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/replace")

    assert response.status_code == 409


def test_replace_matched_state_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="matched")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/replace")

    assert response.status_code == 409


def test_reject_clears_proposed_action(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    verdict_id = _make_verdict(session_factory, job_id, proposed_action={"kind": "replace"})

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/reject")

    assert response.status_code == 200
    assert response.json() == {"result": "quarantine"}
    with session_factory() as session:
        assert session.get(Verdict, verdict_id).proposed_action is None
    assert _get_job(session_factory, job_id).status == "quarantine"


# -- park / unpark / rerun --------------------------------------------------


def test_park_unpark_happy_path(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")

    with TestClient(app) as client:
        park_response = client.post(f"{API_PREFIX}/jobs/{job_id}/park")
        assert park_response.json() == {"result": "hold"}
        unpark_response = client.post(f"{API_PREFIX}/jobs/{job_id}/unpark")
        assert unpark_response.json() == {"result": "pending"}


def test_park_on_active_job_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")
    with session_factory() as session:
        claim_next(session, "worker-1")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/park")

    assert response.status_code == 409


def test_rerun_from_error_to_pending(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="error", attempts=3)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/rerun")

    assert response.status_code == 200
    assert response.json() == {"result": "pending"}
    reloaded = _get_job(session_factory, job_id)
    assert reloaded.status == "pending"
    assert reloaded.attempts == 0


def test_rerun_on_pending_job_409(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="pending")

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/rerun")

    assert response.status_code == 409


# -- backfill -----------------------------------------------------------


def test_backfill_invokes_discoverer(app, monkeypatch):
    runtime = app.state.instances["main"]
    backfill_mock = AsyncMock(return_value=DiscoveryResult(created=7, skipped=2))
    monkeypatch.setattr(runtime.discoverer, "backfill_step", backfill_mock)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/instances/main/backfill", json={"batch_size": 10})

    assert response.status_code == 200
    assert response.json() == {"created": 7, "skipped": 2}
    backfill_mock.assert_awaited_once_with(10, reset=False, series_id=None)


def test_backfill_passes_reset_and_series_id_through(app, monkeypatch):
    runtime = app.state.instances["main"]
    backfill_mock = AsyncMock(return_value=DiscoveryResult(created=0, skipped=0))
    monkeypatch.setattr(runtime.discoverer, "backfill_step", backfill_mock)

    with TestClient(app) as client:
        response = client.post(
            f"{API_PREFIX}/instances/main/backfill",
            json={"batch_size": 10, "reset": True, "series_id": 42},
        )

    assert response.status_code == 200
    backfill_mock.assert_awaited_once_with(10, reset=True, series_id=42)


def test_backfill_unknown_instance_404(app):
    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/instances/nope/backfill", json={"batch_size": 10})
    assert response.status_code == 404


# -- SSE ------------------------------------------------------------------
#
# httpx's ASGITransport (this version) fully drains an ASGI app's response
# before returning anything — incompatible with an infinite SSE generator.
# Starlette's TestClient streams for real, but runs the ASGI app on a
# separate portal thread, and `EventBus.publish()` (asyncio.Queue.put_nowait
# under the hood) isn't safe to call cross-thread — a publish from the test's
# own thread just doesn't reliably wake the subscriber's `queue.get()`. Both
# gaps are test-harness artifacts, not a production concern (the worker pool
# publishes from the same event loop the app runs on, under uvicorn). So:
# a real HTTP-level smoke test for wiring/content-type, plus same-event-loop
# tests of `_event_stream` (the exact function `GET /events` calls) for the
# publish -> job_update behavior and the periodic stats/heartbeat behavior.


def test_sse_events_http_smoke(app, monkeypatch):
    # Starlette's StreamingResponse doesn't hand back headers until the
    # first chunk is ready, so a real client blocks for STATS_INTERVAL_S
    # (15s) if nothing is published first — shrink it so this HTTP-level
    # wiring check (route mounted, correct content-type) stays fast.
    monkeypatch.setattr("impostarr.api.routes.STATS_INTERVAL_S", 0.01)
    with TestClient(app) as client, client.stream("GET", f"{API_PREFIX}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


class _FakeAppState:
    def __init__(self, app):
        self.event_bus = app.state.event_bus
        self.session_factory = app.state.session_factory


class _FakeRequest:
    """Just enough of `Request` for `_event_stream`, which only reads
    `request.app.state.{event_bus,session_factory}`."""

    def __init__(self, app):
        self.app = type("App", (), {"state": _FakeAppState(app)})()


@pytest.mark.asyncio
async def test_sse_event_stream_yields_published_job_update(app):
    from impostarr.api.routes import _event_stream

    stream = _event_stream(_FakeRequest(app))

    async def publish_soon():
        await asyncio.sleep(0.05)
        app.state.event_bus.publish({"type": "job_update", "job_id": 123, "status": "matched"})

    publisher = asyncio.create_task(publish_soon())

    async def read_job_update() -> dict:
        async for chunk in stream:
            if chunk.startswith("event: job_update"):
                data_line = chunk.splitlines()[1]
                return json.loads(data_line.removeprefix("data:").strip())
        raise AssertionError("stream ended before a job_update event arrived")

    try:
        event = await asyncio.wait_for(read_job_update(), timeout=5)
    finally:
        await publisher
        await stream.aclose()

    assert event == {"type": "job_update", "job_id": 123, "status": "matched"}


@pytest.mark.asyncio
async def test_sse_event_stream_periodic_stats_and_heartbeat(app, monkeypatch):
    monkeypatch.setattr("impostarr.api.routes.STATS_INTERVAL_S", 0.05)
    from impostarr.api.routes import _event_stream

    stream = _event_stream(_FakeRequest(app))
    try:
        stats_chunk = await asyncio.wait_for(stream.__anext__(), timeout=5)
        heartbeat_chunk = await asyncio.wait_for(stream.__anext__(), timeout=5)
    finally:
        await stream.aclose()

    assert stats_chunk.startswith("event: stats")
    stats = json.loads(stats_chunk.splitlines()[1].removeprefix("data:").strip())
    assert set(stats) == {
        "hold", "pending", "active", "matched", "quarantine", "inconclusive", "error", "remediated",
    }
    assert heartbeat_chunk == ": heartbeat\n\n"


# -- trash --------------------------------------------------------------


def test_list_trash_returns_active_items_newest_first(app, tmp_path):
    session_factory = _session_factory(app)
    older_id = _make_trash_item(
        session_factory, tmp_path, trash_path=tmp_path / "trash/main/a-1", series_id=1,
    )
    newer_id = _make_trash_item(
        session_factory, tmp_path, trash_path=tmp_path / "trash/main/b-2", series_id=2,
    )
    with session_factory() as session:
        older = session.get(TrashItem, older_id)
        older.trashed_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/trash")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [newer_id, older_id]
    first = body["items"][0]
    assert set(first) == {
        "id", "instance", "original_path", "trash_path", "series_id", "episode_ids",
        "size", "trashed_at", "expires_at", "expires_in_s",
    }
    assert first["instance"] == "main"
    assert first["expires_in_s"] > 0


def test_list_trash_excludes_already_deleted_items(app, tmp_path):
    session_factory = _session_factory(app)
    active_id = _make_trash_item(session_factory, tmp_path)
    deleted_id = _make_trash_item(
        session_factory, tmp_path, trash_path=tmp_path / "trash/main/x-2",
    )
    with session_factory() as session:
        item = session.get(TrashItem, deleted_id)
        item.deleted_at = datetime.now(UTC)
        item.outcome = "deleted"
        session.commit()

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/trash")

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [active_id]


def test_delete_trash_item_unlinks_and_marks_deleted(app, tmp_path):
    session_factory = _session_factory(app)
    item_id = _make_trash_item(session_factory, tmp_path)
    trash_path = _get_trash_item(session_factory, item_id).trash_path

    with TestClient(app) as client:
        response = client.delete(f"{API_PREFIX}/trash/{item_id}")

    assert response.status_code == 200
    assert response.json() == {"result": "deleted"}
    assert not os.path.exists(trash_path)
    item = _get_trash_item(session_factory, item_id)
    assert item.outcome == "deleted"
    assert item.deleted_at is not None


def test_delete_trash_item_404_when_missing(app):
    with TestClient(app) as client:
        response = client.delete(f"{API_PREFIX}/trash/999999")
    assert response.status_code == 404


def test_delete_trash_item_409_when_already_deleted(app, tmp_path):
    session_factory = _session_factory(app)
    item_id = _make_trash_item(session_factory, tmp_path)
    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        item.deleted_at = datetime.now(UTC)
        item.outcome = "deleted"
        session.commit()

    with TestClient(app) as client:
        response = client.delete(f"{API_PREFIX}/trash/{item_id}")

    assert response.status_code == 409


def test_restore_trash_item_copies_file_back_and_marks_restored(app, tmp_path):
    session_factory = _session_factory(app)
    original_path = tmp_path / "media" / "Show" / "S01E01.mkv"
    item_id = _make_trash_item(session_factory, tmp_path, original_path=original_path)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/trash/{item_id}/restore")

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "restored"
    assert body["original_path"] == str(original_path)
    assert "not re-imported into Sonarr" in body["note"]
    assert original_path.exists()
    item = _get_trash_item(session_factory, item_id)
    assert item.outcome == "restored"
    assert item.deleted_at is not None


def test_restore_trash_item_409_when_original_path_occupied(app, tmp_path):
    session_factory = _session_factory(app)
    original_path = tmp_path / "media" / "Show" / "S01E01.mkv"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(b"already there")
    item_id = _make_trash_item(session_factory, tmp_path, original_path=original_path)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/trash/{item_id}/restore")

    assert response.status_code == 409
    item = _get_trash_item(session_factory, item_id)
    assert item.outcome is None  # restore never partially applied
