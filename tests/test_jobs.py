from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from impostarr.config import Settings
from impostarr.db import init_db, make_session_factory
from impostarr.jobs import (
    InvalidTransition,
    claim_next,
    create_job,
    heartbeat,
    park,
    reap_stale,
    release,
    unpark,
)
from impostarr.models import File, Instance, Job


@pytest.fixture
def engine(tmp_path):
    settings = Settings(state_dir=tmp_path)
    return init_db(settings)


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


def _make_file(session, *, episode_file_id=1):
    instance = session.query(Instance).first()
    if instance is None:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
    file = File(
        instance_id=instance.id,
        sonarr_path=f"/tv/Show/S01E{episode_file_id:02d}.mkv",
        local_path=f"/media/tv/Show/S01E{episode_file_id:02d}.mkv",
        size=1,
        content_hash=f"hash{episode_file_id}",
        series_id=1,
        episode_ids=[episode_file_id],
        episode_file_id=episode_file_id,
        quality={},
        languages=[],
    )
    session.add(file)
    session.commit()
    return file.id


def test_create_job_defaults_to_pending(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id)
        assert job.status == "pending"


def test_create_job_parked_starts_hold(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id, parked=True)
        assert job.status == "hold"


def test_claim_next_returns_none_when_no_pending_jobs(session_factory):
    with session_factory() as session:
        assert claim_next(session, "worker-1") is None


def test_claim_next_claims_oldest_pending_job_first(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        now = datetime.now(UTC)
        older = Job(file_id=file_id, status="pending", created_at=now - timedelta(minutes=5))
        newer = Job(file_id=file_id, status="pending", created_at=now)
        session.add_all([newer, older])
        session.commit()

        claimed = claim_next(session, "worker-1")

        assert claimed.id == older.id


def test_claim_next_ties_break_by_id_ascending(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        now = datetime.now(UTC)
        first = Job(file_id=file_id, status="pending", created_at=now)
        session.add(first)
        session.commit()
        second = Job(file_id=file_id, status="pending", created_at=now)
        session.add(second)
        session.commit()
        assert first.id < second.id

        claimed = claim_next(session, "worker-1")

        assert claimed.id == first.id


def test_claim_next_sets_active_status_and_lease_fields(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)

        claimed = claim_next(session, "worker-1")

        assert claimed.status == "active"
        assert claimed.claimed_by == "worker-1"
        assert claimed.claimed_at is not None
        assert claimed.heartbeat_at is not None


def test_claim_next_concurrent_claimers_exactly_one_wins(session_factory):
    with session_factory() as setup_session:
        file_id = _make_file(setup_session)
        job = create_job(setup_session, file_id)
        job_id = job.id

    barrier = threading.Barrier(2)
    results: dict[str, Job | None] = {}

    def attempt(worker_id):
        with session_factory() as session:
            barrier.wait()
            results[worker_id] = claim_next(session, worker_id)

    threads = [threading.Thread(target=attempt, args=(f"worker-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [job for job in results.values() if job is not None]
    assert len(winners) == 1
    assert winners[0].id == job_id


def test_claim_next_concurrent_claimers_each_get_distinct_job(session_factory):
    with session_factory() as setup_session:
        file_id = _make_file(setup_session)
        job_a = create_job(setup_session, file_id)
        job_b = create_job(setup_session, file_id)
        ids = {job_a.id, job_b.id}

    barrier = threading.Barrier(2)
    results: dict[str, Job | None] = {}

    def attempt(worker_id):
        with session_factory() as session:
            barrier.wait()
            results[worker_id] = claim_next(session, worker_id)

    threads = [threading.Thread(target=attempt, args=(f"worker-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(job is not None for job in results.values())
    claimed_ids = {job.id for job in results.values()}
    assert claimed_ids == ids


def test_heartbeat_bumps_heartbeat_at_on_active_job(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        job = claim_next(session, "worker-1")
        original_heartbeat = job.heartbeat_at

        heartbeat(session, job)

        assert job.heartbeat_at >= original_heartbeat


def test_heartbeat_on_pending_job_raises_invalid_transition(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id)

        with pytest.raises(InvalidTransition):
            heartbeat(session, job)


def test_release_active_to_terminal_status_clears_lease(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        job = claim_next(session, "worker-1")

        released = release(session, job, "matched")

        assert released.status == "matched"
        assert released.claimed_by is None
        assert released.claimed_at is None
        assert released.heartbeat_at is None


def test_release_active_back_to_pending_for_requeue(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        job = claim_next(session, "worker-1")

        released = release(session, job, "pending")

        assert released.status == "pending"
        assert released.claimed_by is None


def test_release_on_hold_job_raises_invalid_transition(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id, parked=True)

        with pytest.raises(InvalidTransition):
            release(session, job, "matched")


def test_reap_stale_requeues_and_increments_attempts(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        job = claim_next(session, "worker-1")
        job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        session.commit()

        reaped = reap_stale(session, lease_timeout_s=60)

        assert reaped == 1
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.claimed_by is None
        assert job.claimed_at is None
        assert job.heartbeat_at is None


def test_reap_stale_marks_error_when_attempts_would_exceed_cap(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id)
        job.attempts = 3
        session.commit()
        claimed = claim_next(session, "worker-1")
        claimed.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        session.commit()

        reaped = reap_stale(session, lease_timeout_s=60)

        assert reaped == 1
        assert claimed.status == "error"
        assert claimed.attempts == 4


def test_reap_stale_ignores_active_jobs_within_lease(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        claim_next(session, "worker-1")

        reaped = reap_stale(session, lease_timeout_s=3600)

        assert reaped == 0


def test_park_unpark_round_trip(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id)

        parked = park(session, job)
        assert parked.status == "hold"

        unparked = unpark(session, job)
        assert unparked.status == "pending"


def test_park_on_active_job_raises_invalid_transition(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        create_job(session, file_id)
        job = claim_next(session, "worker-1")

        with pytest.raises(InvalidTransition):
            park(session, job)


def test_unpark_on_pending_job_raises_invalid_transition(session_factory):
    with session_factory() as session:
        file_id = _make_file(session)
        job = create_job(session, file_id)

        with pytest.raises(InvalidTransition):
            unpark(session, job)
