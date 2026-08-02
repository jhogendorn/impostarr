from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from impostarr.config import Settings, SonarrInstance
from impostarr.db import init_db, make_session_factory
from impostarr.jobs import claim_next
from impostarr.models import File, Instance, Job, Verdict
from impostarr.remediate import Remediator
from impostarr.sonarr import SonarrClient

BASE_URL = "http://sonarr.test:8989"
API_URL = f"{BASE_URL}/api/v3"
API_KEY = "test-api-key"
WORKER_ID = "worker-1"


# -- fixtures / helpers -----------------------------------------------------


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
        "staging_dir": str(tmp_path / "staging"),
    }
    defaults.update(overrides)
    return SonarrInstance(**defaults)


def make_client(dry_run: bool = False) -> SonarrClient:
    return SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0), dry_run=dry_run)


def make_active_job(
    session_factory,
    tmp_path,
    *,
    history_id: int | None = 500,
    episode_ids: list[int] | None = None,
    series_id: int = 42,
    episode_file_id: int = 9001,
    content: bytes = b"episode content",
) -> tuple[int, Path]:
    """Create an instance/file/job/verdict, claim the job (active +
    WORKER_ID), and return (job_id, local_path)."""
    episode_ids = episode_ids if episode_ids is not None else [555]
    local_path = tmp_path / "media" / "Show" / "S01E02.mkv"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)

    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL)
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E02.mkv",
            local_path=str(local_path),
            size=len(content),
            content_hash="abc123",
            series_id=series_id,
            episode_ids=episode_ids,
            episode_file_id=episode_file_id,
            quality={"quality": {"id": 7}},
            languages=[{"id": 1}],
            history_id=history_id,
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.flush()
        verdict = Verdict(job_id=job.id, outcome="quarantine", source="auto")
        session.add(verdict)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        claim_next(session, WORKER_ID)

    return job_id, local_path


def get_job_status(session_factory, job_id: int) -> str:
    with session_factory() as session:
        return session.get(Job, job_id).status


def get_remediation_log(session_factory, job_id: int) -> list[dict]:
    with session_factory() as session:
        verdict = session.query(Verdict).filter_by(job_id=job_id).one()
        return verdict.remediation_log


def seed_remediation_log(session_factory, job_id: int) -> None:
    """Pre-populate the job's verdict.remediation_log, simulating a prior
    interrupted remediation attempt."""
    with session_factory() as session:
        verdict = session.query(Verdict).filter_by(job_id=job_id).one()
        verdict.remediation_log.append(
            {"step": "delete_episode_file", "ok": True, "detail": "prior attempt", "ts": "x"}
        )
        session.commit()


def episode_json(id_, *, season_number=1, episode_number=1, episode_file_id=0, has_file=True):
    return {
        "id": id_,
        "seasonNumber": season_number,
        "episodeNumber": episode_number,
        "episodeFileId": episode_file_id,
        "hasFile": has_file,
    }


# -- replace ------------------------------------------------------------


@respx.mock
async def test_replace_with_grab_history_calls_three_apis_in_order_and_remediates(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)

    history_route = respx.post(f"{API_URL}/history/failed/500").mock(
        return_value=httpx.Response(200, json={})
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json={})
    )
    command_route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.replace(job, WORKER_ID)

    assert history_route.called
    assert delete_route.called
    assert command_route.called
    assert respx.calls.call_count == 3
    assert command_route.calls.last.request.content

    body = json.loads(command_route.calls.last.request.content)
    assert body["name"] == "EpisodeSearch"
    assert body["episodeIds"] == [555]

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 3
    assert all(step["ok"] for step in log)
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_without_grab_history_calls_two_apis_and_notes_unblocklistable(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=None)

    history_route = respx.post(f"{API_URL}/history/failed/500")
    delete_route = respx.delete(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json={})
    )
    command_route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.replace(job, WORKER_ID)

    assert not history_route.called
    assert delete_route.called
    assert command_route.called
    assert respx.calls.call_count == 2

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 3
    assert log[0]["ok"] is True
    assert "cannot be blocklisted" in log[0]["detail"]
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_delete_failure_stops_sequence_and_quarantines(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)

    respx.post(f"{API_URL}/history/failed/500").mock(return_value=httpx.Response(200, json={}))
    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(500, text="boom"))
    command_route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.replace(job, WORKER_ID)

    assert not command_route.called

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 2
    assert log[0]["ok"] is True
    assert log[1]["ok"] is False
    assert get_job_status(session_factory, job_id) == "quarantine"


# -- remap ------------------------------------------------------------


@respx.mock
async def test_remap_happy_path_hardlinks_and_imports(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    job_id, local_path = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    episodes_route = respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json={})
    )
    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    manualimport_route = respx.get(f"{API_URL}/manualimport").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "path": str(staged_path),
                    "series": {"id": 42},
                    "episodes": [],
                    "quality": {"quality": {"id": 7}},
                    "languages": [{"id": 1}],
                }
            ],
        )
    )
    command_route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 2, "name": "ManualImport"})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert staged_path.exists()
    assert staged_path.stat().st_ino == local_path.stat().st_ino

    assert respx.calls.call_count == 4
    names = [str(c.request.url) for c in respx.calls]
    assert "episode" in names[0] and "seriesId" in names[0]
    assert "episodefile/9001" in names[1]
    assert "manualimport" in names[2]
    assert names[3].endswith("/command")


    body = json.loads(command_route.calls.last.request.content)
    assert body["name"] == "ManualImport"
    assert body["importMode"] == "move"
    assert body["files"][0]["episodeIds"] == [777]
    assert body["files"][0]["seriesId"] == 42
    assert body["files"][0]["path"] == str(staged_path)

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 4
    assert all(step["ok"] for step in log)
    assert get_job_status(session_factory, job_id) == "remediated"

    assert episodes_route.called
    assert delete_route.called
    assert manualimport_route.called


@respx.mock
async def test_remap_manual_import_failure_keeps_staging_file_and_quarantines(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API_URL}/manualimport").mock(return_value=httpx.Response(500, text="boom"))

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    assert staged_path.exists()

    log = get_remediation_log(session_factory, job_id)
    failed_steps = [s for s in log if not s["ok"]]
    assert len(failed_steps) == 1
    assert str(staged_path) in failed_steps[0]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"


@respx.mock
async def test_remap_occupied_target_refuses_with_zero_mutating_calls(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=9002, has_file=True
                ),
            ],
        )
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001")
    manualimport_route = respx.get(f"{API_URL}/manualimport")
    command_route = respx.post(f"{API_URL}/command")

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert not delete_route.called
    assert not manualimport_route.called
    assert not command_route.called

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    assert not staged_path.exists()

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 1
    assert log[0]["ok"] is False
    assert get_job_status(session_factory, job_id) == "quarantine"


@respx.mock
async def test_remap_cross_device_fallback_uses_copy(tmp_path, session_factory, monkeypatch):
    cfg = make_instance_cfg(tmp_path)
    job_id, local_path = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200, json={}))
    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    respx.get(f"{API_URL}/manualimport").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "path": str(staged_path),
                    "series": {"id": 42},
                    "episodes": [],
                    "quality": {"quality": {"id": 7}},
                    "languages": [{"id": 1}],
                }
            ],
        )
    )
    respx.post(f"{API_URL}/command").mock(return_value=httpx.Response(200, json={}))

    def fake_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", fake_link)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert staged_path.exists()
    assert staged_path.read_bytes() == local_path.read_bytes()
    assert get_job_status(session_factory, job_id) == "remediated"


# -- no verdict row ------------------------------------------------------


async def test_replace_raises_without_verdict_row(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL)
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E02.mkv",
            local_path=str(tmp_path / "nope.mkv"),
            size=1,
            content_hash="x",
            series_id=42,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        claim_next(session, WORKER_ID)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        with pytest.raises(ValueError):
            await remediator.replace(job, WORKER_ID)


# -- interruption guard ---------------------------------------------------


@respx.mock
async def test_replace_interruption_guard_quarantines_with_zero_api_calls(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)
    seed_remediation_log(session_factory, job_id)

    # No routes mocked at all: any API call would raise a respx "not mocked"
    # error, which would fail the test just as surely as an explicit count
    # assertion.
    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.replace(job, WORKER_ID)

    assert respx.calls.call_count == 0
    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 2  # seeded entry + guard entry
    assert log[-1]["ok"] is False
    assert "interrupted" in log[-1]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"


@respx.mock
async def test_remap_interruption_guard_quarantines_with_zero_api_calls(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    seed_remediation_log(session_factory, job_id)
    target_ids = frozenset({777})

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert respx.calls.call_count == 0
    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 2
    assert log[-1]["ok"] is False
    assert "interrupted" in log[-1]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"


# -- partial episode-id resolution -----------------------------------------


@respx.mock
async def test_remap_partial_episode_resolution_refuses_with_zero_mutating_calls(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    # 9999 is not present in the episodes response below -> unresolvable.
    target_ids = frozenset({777, 9999})

    episodes_route = respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001")
    manualimport_route = respx.get(f"{API_URL}/manualimport")
    command_route = respx.post(f"{API_URL}/command")

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert episodes_route.called
    assert not delete_route.called
    assert not manualimport_route.called
    assert not command_route.called

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    assert not staged_path.exists()

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 1
    assert log[0]["ok"] is False
    assert "9999" in log[0]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"


# -- staging directory / hardlink error handling ---------------------------


@respx.mock
async def test_remap_hardlink_non_exdev_failure_includes_staging_path(
    tmp_path, session_factory, monkeypatch
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001")

    def fake_link(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "link", fake_link)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert not delete_route.called

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 1
    assert log[0]["ok"] is False
    assert str(staged_path) in log[0]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"


# -- dry_run --------------------------------------------------------------
#
# No mutating route (delete/history/manualimport-POST) is mocked in any of
# these: the client's own dry-run stubs make those calls no-ops, so a stray
# real HTTP call to one would raise via respx's unmocked-request error just
# as surely as an explicit call-count assertion.


@respx.mock
async def test_replace_dry_run_zero_mutating_calls_and_remediates(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client(dry_run=True) as client:
        remediator = Remediator(client, cfg, session_factory, dry_run=True)
        await remediator.replace(job, WORKER_ID)

    assert respx.calls.call_count == 0

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 3
    assert all(step["ok"] for step in log)
    assert all(step["detail"].startswith("DRY-RUN: ") for step in log)
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_remap_dry_run_skips_filesystem_and_manual_import_but_remediates(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, local_path = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    # Only a GET is mocked (episode resolution): delete_episode_file is a
    # client-level dry-run no-op, and dry-run remap skips the
    # manualimport GET + command POST entirely.
    episodes_route = respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client(dry_run=True) as client:
        remediator = Remediator(client, cfg, session_factory, dry_run=True)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert episodes_route.called
    assert respx.calls.call_count == 1

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    assert not staged_path.exists()
    assert not Path(cfg.staging_dir).exists()  # never created
    assert local_path.exists()  # original file untouched

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 3
    steps = [entry["step"] for entry in log]
    assert steps == ["hardlink", "delete_episode_file", "manual_import"]
    assert all(step["ok"] for step in log)
    assert all(step["detail"].startswith("DRY-RUN: ") for step in log)
    assert "imported staged file as S01E03" in log[2]["detail"]
    assert "777" in log[2]["detail"]
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_remap_copy2_failure_after_exdev_quarantines_with_staging_path(
    tmp_path, session_factory, monkeypatch
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                episode_json(
                    777, season_number=1, episode_number=3, episode_file_id=0, has_file=False
                ),
            ],
        )
    )
    delete_route = respx.delete(f"{API_URL}/episodefile/9001")

    def fake_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fake_copy2(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "link", fake_link)
    monkeypatch.setattr("impostarr.remediate.shutil.copy2", fake_copy2)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert not delete_route.called

    staged_path = Path(cfg.staging_dir) / "S01E03.mkv"
    assert not staged_path.exists()

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 1
    assert log[0]["ok"] is False
    assert str(staged_path) in log[0]["detail"]
    assert get_job_status(session_factory, job_id) == "quarantine"
