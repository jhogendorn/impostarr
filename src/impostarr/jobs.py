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

Lease fencing: `heartbeat` and `release` require the caller's `worker_id`
and only touch a row via `WHERE id=:id AND status='active' AND
claimed_by=:worker_id`. A stale worker (its lease already reaped and
re-claimed by someone else) gets rowcount 0 and `LeaseLost` instead of
silently clobbering the new claimant. `park`/`unpark` have no claimant to
fence on but are still conditioned on their expected source status via
rowcount, so a concurrent claim racing a park can't be silently overwritten
either (raises `InvalidTransition` in that case).

Commit semantics: every function in this module commits the session itself
(or rolls back on a lost race/lease) before returning. Do not share a
session across one of these calls and other uncommitted, unrelated work —
that work will be committed or rolled back as a side effect.
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

# Allowed `from status -> {to statuses}` for the queue state machine. Single
# source of truth for every status change in this module.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "hold": frozenset({"pending"}),
    "pending": frozenset({"hold", "active"}),
    "active": frozenset({"pending"} | TERMINAL_STATUSES),
}


class InvalidTransition(Exception):
    """Raised when a job status change doesn't match the queue state machine."""


class LeaseLost(Exception):
    """Raised when a worker no longer holds the active lease it thinks it does
    (reaped for a stale heartbeat and possibly re-claimed by another worker)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_transition(from_status: str, new_status: str, job_id: int) -> None:
    """Validate `from_status -> new_status` against `VALID_TRANSITIONS`.

    Pure validation, no mutation — callers apply the status themselves once
    the corresponding DB write (fenced or not) has actually succeeded.
    """
    allowed = VALID_TRANSITIONS.get(from_status, frozenset())
    if new_status not in allowed:
        raise InvalidTransition(
            f"cannot transition job {job_id} from {from_status!r} to {new_status!r}"
        )


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

        _check_transition(job.status, "active", job.id)
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


def heartbeat(session: Session, job: Job, worker_id: str) -> None:
    """Bump `heartbeat_at`. Requires `worker_id` to still hold the active
    lease (`status='active' AND claimed_by=worker_id` in the DB); otherwise
    the lease was reaped or stolen and `LeaseLost` is raised."""
    now = _utcnow()
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "active", Job.claimed_by == worker_id)
        .values(heartbeat_at=now)
    )
    if result.rowcount == 0:
        session.rollback()
        raise LeaseLost(f"job {job.id} lease lost (heartbeat) for worker {worker_id!r}")
    job.heartbeat_at = now
    session.commit()


def release(
    session: Session, job: Job, new_status: str, worker_id: str, **result_fields: object
) -> Job:
    """Move an `active` job to a terminal status, or back to `pending` to requeue.

    `new_status` is validated against `VALID_TRANSITIONS` for the job's
    current (in-memory) status first, so a job that's terminal or was never
    active raises `InvalidTransition` regardless of who's asking. Only once
    that passes is the lease-fenced UPDATE attempted
    (`status='active' AND claimed_by=worker_id`); a zero-row match there
    means the lease was reaped or stolen out from under `worker_id`, and
    `LeaseLost` is raised instead of silently clobbering whoever holds it
    now.

    Clears lease fields (`claimed_by`, `claimed_at`, `heartbeat_at`). Any
    `result_fields` are set as attributes on `job` for forward compatibility
    with future result-carrying columns.
    """
    _check_transition(job.status, new_status, job.id)
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "active", Job.claimed_by == worker_id)
        .values(status=new_status, claimed_by=None, claimed_at=None, heartbeat_at=None)
    )
    if result.rowcount == 0:
        session.rollback()
        raise LeaseLost(f"job {job.id} lease lost (release) for worker {worker_id!r}")
    job.status = new_status
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
        target_status = "error" if job.attempts > MAX_ATTEMPTS else "pending"
        _check_transition(job.status, target_status, job.id)
        job.status = target_status
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
    session.commit()
    return len(stale_jobs)


def park(session: Session, job: Job) -> Job:
    """`pending` -> `hold`. Fenced on the job still being `pending` in the
    DB, so a concurrent claim racing a park can't be silently overwritten."""
    _check_transition(job.status, "hold", job.id)
    result = session.execute(
        update(Job).where(Job.id == job.id, Job.status == "pending").values(status="hold")
    )
    if result.rowcount == 0:
        session.rollback()
        raise InvalidTransition(f"job {job.id} was no longer pending when parking")
    job.status = "hold"
    session.commit()
    return job


def unpark(session: Session, job: Job) -> Job:
    """`hold` -> `pending`. Fenced on the job still being `hold` in the DB."""
    _check_transition(job.status, "pending", job.id)
    result = session.execute(
        update(Job).where(Job.id == job.id, Job.status == "hold").values(status="pending")
    )
    if result.rowcount == 0:
        session.rollback()
        raise InvalidTransition(f"job {job.id} was no longer on hold when unparking")
    job.status = "pending"
    session.commit()
    return job
