"""DB-backed job queue state machine over the `Job` model.

Sync SQLAlchemy sessions throughout, deliberately: the async worker pool
(later tasks) runs these calls in threads rather than this module speaking
async itself. The claim mechanism is the seam for future remote workers, so
it is a pure DB operation with no in-process locking (a `threading.Lock`
would not coordinate across multiple worker processes/hosts).

Claim mechanism: `claim_next` uses a SELECT-then-conditional-UPDATE loop —
SELECT the oldest pending job id, then
`UPDATE jobs SET status='active', ... WHERE id=:id AND status='pending'`.
SQLite has no `UPDATE ... ORDER BY ... LIMIT`, so the ordering is resolved
in the SELECT. The UPDATE re-checks `status='pending'` so a second claimer
who read the same candidate loses the race (rowcount 0) and retries against
the next-oldest still-pending job. SQLite serializes writers at the
database level (WAL, single writer; `db.make_engine` sets a 30s busy
timeout), so the UPDATE's rowcount is a reliable arbiter of exactly one
winner even under concurrent sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from impostarr.models import Job

MAX_ATTEMPTS = 3

TERMINAL_STATUSES = frozenset(
    {"matched", "quarantine", "inconclusive", "error", "remediated"}
)

# Allowed `from status -> {to statuses}` for the queue state machine.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "hold": frozenset({"pending"}),
    "pending": frozenset({"hold", "active"}),
    "active": frozenset({"pending"} | TERMINAL_STATUSES),
}


class InvalidTransition(Exception):
    """Raised when a job status change doesn't match the queue state machine."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _transition(job: Job, new_status: str) -> None:
    allowed = VALID_TRANSITIONS.get(job.status, frozenset())
    if new_status not in allowed:
        raise InvalidTransition(
            f"cannot transition job {job.id} from {job.status!r} to {new_status!r}"
        )
    job.status = new_status


def create_job(session: Session, file_id: int, *, parked: bool = False) -> Job:
    """Create a job in `pending`, or `hold` when `parked` (rate-limited/backfill)."""
    job = Job(file_id=file_id, status="hold" if parked else "pending")
    session.add(job)
    session.commit()
    return job


def claim_next(session: Session, worker_id: str) -> Job | None:
    """Atomically claim the oldest pending job. See module docstring for the
    chosen SELECT-then-conditional-UPDATE mechanism and its concurrency
    guarantee.

    The SELECT loads the full ORM entity (not just its id) so that on a win
    we can set the lease fields on that same in-memory object rather than
    re-reading the row from SQLite — SQLite's DateTime storage does not
    round-trip `tzinfo`, so re-reading would hand back naive datetimes
    while every other timestamp in this codebase stays timezone-aware.
    """
    while True:
        job = session.execute(
            select(Job)
            .where(Job.status == "pending")
            .order_by(Job.created_at.asc(), Job.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return None

        now = _utcnow()
        result = session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "pending")
            .values(status="active", claimed_by=worker_id, claimed_at=now, heartbeat_at=now)
        )
        if result.rowcount == 1:
            job.status = "active"
            job.claimed_by = worker_id
            job.claimed_at = now
            job.heartbeat_at = now
            session.commit()
            return job
        # Lost the race for this candidate; release our txn and retry.
        session.rollback()


def heartbeat(session: Session, job: Job) -> None:
    """Bump `heartbeat_at`. Only valid while the job is `active`."""
    if job.status != "active":
        raise InvalidTransition(f"cannot heartbeat job {job.id} in status {job.status!r}")
    job.heartbeat_at = _utcnow()
    session.commit()


def release(session: Session, job: Job, new_status: str, **result_fields: object) -> Job:
    """Move an `active` job to a terminal status, or back to `pending` to requeue.

    Clears lease fields (`claimed_by`, `claimed_at`, `heartbeat_at`). Any
    `result_fields` are set as attributes on `job` for forward compatibility
    with future result-carrying columns.
    """
    _transition(job, new_status)
    job.claimed_by = None
    job.claimed_at = None
    job.heartbeat_at = None
    for key, value in result_fields.items():
        setattr(job, key, value)
    session.commit()
    return job


def reap_stale(session: Session, lease_timeout_s: float) -> int:
    """Requeue `active` jobs whose heartbeat is older than `lease_timeout_s`.

    Increments `attempts`; a job whose attempts would exceed `MAX_ATTEMPTS`
    goes to `error` instead of back to `pending`. Returns the count reaped.
    """
    cutoff = _utcnow() - timedelta(seconds=lease_timeout_s)
    stale_jobs = (
        session.execute(select(Job).where(Job.status == "active", Job.heartbeat_at < cutoff))
        .scalars()
        .all()
    )
    for job in stale_jobs:
        job.attempts += 1
        job.status = "error" if job.attempts > MAX_ATTEMPTS else "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
    session.commit()
    return len(stale_jobs)


def park(session: Session, job: Job) -> Job:
    """`pending` -> `hold`."""
    _transition(job, "hold")
    session.commit()
    return job


def unpark(session: Session, job: Job) -> Job:
    """`hold` -> `pending`."""
    _transition(job, "pending")
    session.commit()
    return job
