from __future__ import annotations

import errno
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from impostarr import jobs
from impostarr.config import Settings, SonarrInstance, TrashConfig
from impostarr.db import init_db, make_session_factory
from impostarr.jobs import claim_next
from impostarr.models import File, Instance, Job, TrashItem, Verdict
from impostarr.remediate import MUTATING_STEPS, Remediator, _is_mutating_entry, _was_interrupted
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


def get_trash_items(session_factory, job_id: int) -> list[TrashItem]:
    with session_factory() as session:
        return session.query(TrashItem).filter_by(job_id=job_id).all()


def seed_remediation_log(session_factory, job_id: int) -> None:
    """Pre-populate the job's verdict.remediation_log, simulating a prior
    interrupted remediation attempt."""
    with session_factory() as session:
        verdict = session.query(Verdict).filter_by(job_id=job_id).one()
        verdict.remediation_log.append(
            {"step": "delete_episode_file", "ok": True, "detail": "prior attempt", "ts": "x"}
        )
        session.commit()


def seed_dry_run_log(session_factory, job_id: int) -> None:
    """Pre-populate the job's verdict.remediation_log with an all-DRY-RUN
    log, simulating a job that fully "remediated" under dry_run and is now
    being retried for real (e.g. dry_run flipped off) against the same
    verdict row — none of these entries touched anything, so they must not
    trip the interruption guard on the real run."""
    with session_factory() as session:
        verdict = session.query(Verdict).filter_by(job_id=job_id).one()
        verdict.remediation_log.extend(
            [
                {
                    "step": "mark_history_failed", "ok": True,
                    "detail": "DRY-RUN: history_id=500", "ts": "x",
                },
                {
                    "step": "delete_episode_file", "ok": True,
                    "detail": "DRY-RUN: removed file via Sonarr (episode_file_id=9001)", "ts": "x",
                },
                {
                    "step": "episode_search", "ok": True,
                    "detail": "DRY-RUN: episode_ids=[555]", "ts": "x",
                },
            ]
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


# -- guard keys on mutating steps, not mere non-emptiness -------------------
#
# Regression coverage for the bug where a refusal-only or all-dry-run log
# permanently dead-ended a job: the guard used to trip on ANY non-empty
# remediation_log, so `occupied_check`/`episode_resolution` refusals (which
# make zero API/fs calls) or a fully-dry-run log left a stale entry that
# blocked every subsequent approve-retry, even after the operator fixed the
# underlying problem.


def test_is_mutating_entry_true_for_a_real_successful_mutation():
    entry = {
        "step": "delete_episode_file", "ok": True,
        "detail": "removed file via Sonarr (episode_file_id=9001)",
    }
    assert _is_mutating_entry(entry) is True
    assert entry["step"] in MUTATING_STEPS


def test_is_mutating_entry_false_for_refusal_and_guard_steps():
    for step in ("occupied_check", "episode_resolution", "interruption_guard"):
        assert step not in MUTATING_STEPS
        assert _is_mutating_entry({"step": step, "ok": False, "detail": "refused"}) is False


def test_is_mutating_entry_false_for_dry_run_entries():
    entry = {
        "step": "delete_episode_file", "ok": True,
        "detail": "DRY-RUN: removed file via Sonarr (episode_file_id=9001)",
    }
    assert _is_mutating_entry(entry) is False


def test_is_mutating_entry_false_for_unblocklistable_note():
    # Shares the "mark_history_failed" step name with the real blocklist
    # call but never makes an API call — told apart by detail text.
    entry = {
        "step": "mark_history_failed", "ok": True,
        "detail": (
            "no download history exists for this file, so the release cannot be "
            "blocklisted — deleting and re-searching only"
        ),
    }
    assert _is_mutating_entry(entry) is False


def test_is_mutating_entry_false_for_a_failed_mutating_step():
    # A "trash"/"hardlink" attempt that failed didn't durably touch
    # anything (or failed atomically enough that retrying doesn't double
    # up) — see the module docstring's reasoning.
    entry = {"step": "trash", "ok": False, "detail": "boom; trash_path=/x"}
    assert _is_mutating_entry(entry) is False


def test_was_interrupted_true_only_when_a_mutating_entry_is_present():
    assert _was_interrupted([]) is False
    assert _was_interrupted([{"step": "occupied_check", "ok": False, "detail": "x"}]) is False
    assert (
        _was_interrupted([{"step": "delete_episode_file", "ok": True, "detail": "removed"}])
        is True
    )


@respx.mock
async def test_remap_retries_and_proceeds_after_occupied_refusal_once_conflict_resolved(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(
        session_factory, tmp_path, episode_ids=[555], series_id=42, episode_file_id=9001
    )
    target_ids = frozenset({777})

    # First call: target occupied. Second call (after the operator frees
    # the slot): target free -> remap should proceed all the way through.
    respx.get(f"{API_URL}/episode", params={"seriesId": "42"}).mock(
        side_effect=[
            httpx.Response(
                200,
                json=[
                    episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                    episode_json(
                        777, season_number=1, episode_number=3, episode_file_id=9002,
                        has_file=True,
                    ),
                ],
            ),
            httpx.Response(
                200,
                json=[
                    episode_json(555, episode_number=2, episode_file_id=9001, has_file=True),
                    episode_json(
                        777, season_number=1, episode_number=3, episode_file_id=0,
                        has_file=False,
                    ),
                ],
            ),
        ]
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

    # First attempt refuses, quarantines, zero mutating calls made.
    assert not delete_route.called
    log = get_remediation_log(session_factory, job_id)
    assert [entry["step"] for entry in log] == ["occupied_check"]
    assert get_job_status(session_factory, job_id) == "quarantine"

    # Operator frees the target episode and retries (approve -> claim_for_api
    # quarantine->active, then remap again against the SAME verdict row).
    with session_factory() as session:
        db_job = session.get(Job, job_id)
        jobs.claim_for_api(session, db_job, WORKER_ID)
    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory)
        await remediator.remap(job, target_ids, WORKER_ID)

    assert delete_route.called
    assert manualimport_route.called
    assert command_route.called
    log = get_remediation_log(session_factory, job_id)
    assert [entry["step"] for entry in log] == [
        "occupied_check", "hardlink", "delete_episode_file",
        "manual_import_candidates", "execute_manual_import",
    ]
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_retries_and_proceeds_after_trash_failure_once_fixed(
    tmp_path, session_factory, monkeypatch
):
    cfg = make_instance_cfg(tmp_path)
    trash_cfg = TrashConfig(dir=tmp_path / "trash", retention_days=14)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=None)

    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    def fake_link(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "link", fake_link)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)
    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    # First attempt: trash copy fails (permission denied) -> quarantines
    # without ever calling Sonarr's delete. Neither logged entry is a
    # successful mutation (the no-history note never calls anything; the
    # failed trash step didn't durably write anything).
    log = get_remediation_log(session_factory, job_id)
    assert [entry["step"] for entry in log] == ["mark_history_failed", "trash"]
    assert log[1]["ok"] is False
    assert get_job_status(session_factory, job_id) == "quarantine"
    assert get_trash_items(session_factory, job_id) == []

    # Operator fixes the permissions problem and retries.
    monkeypatch.undo()
    with session_factory() as session:
        db_job = session.get(Job, job_id)
        jobs.claim_for_api(session, db_job, WORKER_ID)
    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    log = get_remediation_log(session_factory, job_id)
    assert [entry["step"] for entry in log] == [
        "mark_history_failed", "trash", "mark_history_failed", "trash",
        "delete_episode_file", "episode_search",
    ]
    assert all(entry["ok"] for entry in log[2:])
    assert get_job_status(session_factory, job_id) == "remediated"
    assert len(get_trash_items(session_factory, job_id)) == 1


@respx.mock
async def test_replace_dry_run_log_does_not_block_subsequent_real_retry(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)
    seed_dry_run_log(session_factory, job_id)

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

    async with make_client() as client:  # real (non-dry-run) client/remediator this time
        remediator = Remediator(client, cfg, session_factory)
        await remediator.replace(job, WORKER_ID)

    assert history_route.called
    assert delete_route.called
    assert command_route.called
    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 6  # 3 seeded DRY-RUN entries + 3 real entries
    assert all(entry["detail"].startswith("DRY-RUN: ") for entry in log[:3])
    assert all(not entry["detail"].startswith("DRY-RUN: ") for entry in log[3:])
    assert get_job_status(session_factory, job_id) == "remediated"


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


# -- trash ------------------------------------------------------------------


@respx.mock
async def test_replace_with_trash_enabled_hardlinks_file_and_creates_row(tmp_path, session_factory):
    cfg = make_instance_cfg(tmp_path)
    trash_cfg = TrashConfig(dir=tmp_path / "trash", retention_days=14)
    job_id, local_path = make_active_job(session_factory, tmp_path, history_id=500)

    respx.post(f"{API_URL}/history/failed/500").mock(return_value=httpx.Response(200, json={}))
    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    trash_path = tmp_path / "trash" / "main" / f"{local_path.name}-{job_id}"
    assert trash_path.exists()
    assert trash_path.stat().st_ino == local_path.stat().st_ino  # hardlinked, same content

    items = get_trash_items(session_factory, job_id)
    assert len(items) == 1
    item = items[0]
    assert item.instance == "main"
    assert item.original_path == str(local_path)
    assert item.trash_path == str(trash_path)
    assert item.series_id == 42
    assert item.episode_ids == [555]
    assert item.deleted_at is None
    assert item.outcome is None
    # trashed_at (a column default evaluated at flush) and expires_at
    # (computed slightly earlier, in Remediator.replace) are two
    # independent `datetime.now(UTC)` calls a few microseconds apart, so
    # comparing to timedelta(days=14) tolerates that instead of asserting
    # an exact `.days == 14` (which truncates and can read 13).
    assert abs((item.expires_at - item.trashed_at) - timedelta(days=14)) < timedelta(seconds=5)

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 4
    steps = [entry["step"] for entry in log]
    assert steps == ["mark_history_failed", "trash", "delete_episode_file", "episode_search"]
    assert all(entry["ok"] for entry in log)
    trash_step = log[1]
    assert "copied file to trash" in trash_step["detail"]
    assert str(trash_path) in trash_step["detail"]
    delete_step = log[2]
    assert delete_step["detail"] == (
        f"removed file via Sonarr (episode_file_id=9001; "
        f"copy retained in trash until {item.expires_at.isoformat()})"
    )
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_with_trash_disabled_via_config_matches_no_trash_default(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    trash_cfg = TrashConfig(enabled=False, dir=tmp_path / "trash")
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)

    respx.post(f"{API_URL}/history/failed/500").mock(return_value=httpx.Response(200, json={}))
    respx.delete(f"{API_URL}/episodefile/9001").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    assert not (tmp_path / "trash").exists()
    assert get_trash_items(session_factory, job_id) == []

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 3  # no "trash" step at all
    assert [entry["step"] for entry in log] == [
        "mark_history_failed", "delete_episode_file", "episode_search",
    ]
    assert log[1]["detail"] == "removed file via Sonarr (episode_file_id=9001)"
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_with_trash_dry_run_skips_filesystem_and_row_but_remediates(
    tmp_path, session_factory
):
    cfg = make_instance_cfg(tmp_path)
    trash_cfg = TrashConfig(dir=tmp_path / "trash", retention_days=14)
    job_id, local_path = make_active_job(session_factory, tmp_path, history_id=500)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client(dry_run=True) as client:
        remediator = Remediator(client, cfg, session_factory, dry_run=True, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    assert respx.calls.call_count == 0
    assert not (tmp_path / "trash").exists()
    assert get_trash_items(session_factory, job_id) == []
    assert local_path.exists()  # original untouched

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 4
    steps = [entry["step"] for entry in log]
    assert steps == ["mark_history_failed", "trash", "delete_episode_file", "episode_search"]
    assert all(entry["ok"] for entry in log)
    assert log[1]["detail"].startswith("DRY-RUN: would copy file to trash: ")
    assert log[2]["detail"].startswith("DRY-RUN: removed file via Sonarr")
    assert "copy retained in trash until" in log[2]["detail"]
    assert get_job_status(session_factory, job_id) == "remediated"


@respx.mock
async def test_replace_trash_copy_failure_quarantines_without_calling_sonarr_delete(
    tmp_path, session_factory, monkeypatch
):
    cfg = make_instance_cfg(tmp_path)
    trash_cfg = TrashConfig(dir=tmp_path / "trash", retention_days=14)
    job_id, _ = make_active_job(session_factory, tmp_path, history_id=500)

    respx.post(f"{API_URL}/history/failed/500").mock(return_value=httpx.Response(200, json={}))
    delete_route = respx.delete(f"{API_URL}/episodefile/9001")
    command_route = respx.post(f"{API_URL}/command")

    def fake_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fake_copy2(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "link", fake_link)
    monkeypatch.setattr("impostarr.remediate.shutil.copy2", fake_copy2)

    with session_factory() as db_session:
        job = db_session.get(Job, job_id)

    async with make_client() as client:
        remediator = Remediator(client, cfg, session_factory, trash_cfg=trash_cfg)
        await remediator.replace(job, WORKER_ID)

    assert not delete_route.called  # never asked Sonarr to delete a file we failed to preserve
    assert not command_route.called
    assert get_trash_items(session_factory, job_id) == []

    log = get_remediation_log(session_factory, job_id)
    assert len(log) == 2
    assert log[0]["ok"] is True  # mark_history_failed
    assert log[1]["step"] == "trash"
    assert log[1]["ok"] is False
    assert get_job_status(session_factory, job_id) == "quarantine"
