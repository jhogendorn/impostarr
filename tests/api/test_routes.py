from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from impostarr import jobs
from impostarr.config import Settings, SonarrInstance, WorkersConfig
from impostarr.discovery import Discoverer
from impostarr.jobs import claim_next
from impostarr.main import create_app
from impostarr.models import (
    Asset,
    File,
    FrameHash,
    Instance,
    Job,
    PhashCorpusEntry,
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


# -- job detail / assets -----------------------------------------------------


def test_job_detail_includes_plugin_results_and_verdict(app):
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


def test_job_detail_includes_dupe_info(app):
    session_factory = _session_factory(app)
    instance_id = _make_instance(session_factory)
    file_id = _make_file(session_factory, instance_id)
    job_id = _make_job(session_factory, file_id, status="quarantine")
    dupe_info = {"duplicate_of_file_id": 999, "similarity": 0.95, "sonarr_path": "/tv/Other/S01E01.mkv"}
    _make_verdict(session_factory, job_id, outcome="quarantine", dupe_info=dupe_info)

    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/{job_id}")

    assert response.json()["verdict"]["dupe_info"] == dupe_info


def test_job_detail_404_unknown(app):
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/jobs/999999")
    assert response.status_code == 404


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

    def __init__(self, client, cfg, session_factory, dry_run: bool = False) -> None:
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
    backfill_mock = AsyncMock(return_value=7)
    monkeypatch.setattr(runtime.discoverer, "backfill_step", backfill_mock)

    with TestClient(app) as client:
        response = client.post(f"{API_PREFIX}/instances/main/backfill", json={"batch_size": 10})

    assert response.status_code == 200
    assert response.json() == {"created": 7}
    backfill_mock.assert_awaited_once_with(10)


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
