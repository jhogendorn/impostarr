"""Async worker pool: orchestration only. Claim/heartbeat/process loop,
lease reaper, and per-instance discovery scheduling all live here as thin
glue; the actual pipeline logic is in `pipeline.py` and the queue mechanics
are in `jobs.py`.

Worker identity: `start()` mints a distinct `worker_id` per worker-loop
task (`_mint_worker_id(base, n)`), via a shallow `dataclasses.replace()` of
each instance's `PipelineDeps` with only `worker_id` overridden — the
`session_factory`/`sonarr_client`/`plugins`/`series_cache` (+ its lock)
stay shared references across the copies, so the in-process series-context
cache is still pooled per instance, not fragmented per task. Sharing one
`worker_id` across concurrent worker-loop tasks would reopen the Task 5
clobber *inside* a single pool: task A claims a job, stalls past the lease
timeout, the reaper requeues it, task B (same `worker_id`) reclaims it, and
A's later heartbeat/release would then pass the `claimed_by` fence — meant
to catch exactly this — and clobber B, because the fence can't tell A and B
apart. Distinct per-task ids close that gap; `claim_next`'s atomic UPDATE
already prevents two tasks (same or different ids) from claiming the same
job in the first place.

Trash sweep: `_trash_sweep_loop` runs `trash.sweep_expired` hourly, first run
at startup (unlike `_reaper_loop`, which sleeps before its first pass — the
sweep runs immediately so a long-idle pool doesn't leave expired trash
sitting around until the first hour mark). It runs unconditionally,
independent of `Settings.dry_run`: see `trash.py`'s module docstring for why
(short version: it's Impostarr sweeping its own trash mount, not a
media-library mutation).

Shutdown cancellation: `_run_job` awaits `process_task` directly, so
cancelling the enclosing worker-loop task also cancels `process_task` (task
cancellation propagates to whatever a task is currently awaiting) and
raises `CancelledError` at that same `await` point — indistinguishable by
type from the `CancelledError` `_heartbeat_loop` triggers on `LeaseLost`.
The two must be told apart: swallowing the lease-lost cancellation is
correct (the worker loop should carry on to its next job), but swallowing
an external `stop()` cancellation would leave the worker-loop task running
forever, and `stop()`'s `gather()` would hang waiting for it to finish. The
`lease_lost` event, set only by `_heartbeat_loop` right before it cancels
`process_task`, disambiguates: unset means the cancellation came from
outside, so `_run_job` re-raises to let the worker-loop task actually die.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from impostarr import jobs, trash
from impostarr.discovery import Discoverer
from impostarr.jobs import LeaseLost
from impostarr.models import Job
from impostarr.pipeline import PipelineDeps, process_job

logger = logging.getLogger(__name__)

POLL_EMPTY_SLEEP_S = 5.0
REAP_INTERVAL_S = 60.0
TRASH_SWEEP_INTERVAL_S = 3600.0


def _mint_worker_id(base_worker_id: str, task_index: int) -> str:
    return f"{base_worker_id}-{task_index}"


def _do_heartbeat(deps: PipelineDeps, job_id: int) -> None:
    """Blocking: load the job and heartbeat it in one `to_thread` dispatch
    (session.get is itself a blocking DB call — dispatching only
    `jobs.heartbeat` and doing `session.get` inline on the event loop was
    still blocking it)."""
    with deps.session_factory() as session:
        job = session.get(Job, job_id)
        jobs.heartbeat(session, job, deps.worker_id)


class WorkerPool:
    def __init__(
        self,
        deps_per_instance: dict[str, PipelineDeps],
        discoverers: dict[str, Discoverer],
        pool_size: int,
        lease_timeout_s: float = 300.0,
    ) -> None:
        self.deps_per_instance = deps_per_instance
        self.discoverers = discoverers
        self.pool_size = pool_size
        self.lease_timeout_s = lease_timeout_s
        self._session_factory = next(iter(deps_per_instance.values())).session_factory
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = []
        for n in range(self.pool_size):
            per_task_deps = {
                name: replace(deps, worker_id=_mint_worker_id(deps.worker_id, n))
                for name, deps in self.deps_per_instance.items()
            }
            self._tasks.append(asyncio.create_task(self._worker_loop(per_task_deps)))
        self._tasks.append(asyncio.create_task(self._reaper_loop()))
        self._tasks.append(asyncio.create_task(self._trash_sweep_loop()))
        for name, discoverer in self.discoverers.items():
            interval = self.deps_per_instance[name].instance_cfg.poll_interval_s
            self._tasks.append(asyncio.create_task(self._discovery_loop(name, discoverer, interval)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # -- worker loop -----------------------------------------------------

    async def _worker_loop(self, deps_per_instance: dict[str, PipelineDeps]) -> None:
        instance_names = list(deps_per_instance)
        idx = 0
        while True:
            if not instance_names:
                await asyncio.sleep(POLL_EMPTY_SLEEP_S)
                continue
            claimed = False
            for _ in range(len(instance_names)):
                name = instance_names[idx]
                idx = (idx + 1) % len(instance_names)
                deps = deps_per_instance[name]
                with deps.session_factory() as session:
                    job = await asyncio.to_thread(jobs.claim_next, session, deps.worker_id)
                if job is not None:
                    claimed = True
                    await self._run_job(job.id, deps)
                    break
            if not claimed:
                await asyncio.sleep(POLL_EMPTY_SLEEP_S)

    async def _run_job(self, job_id: int, deps: PipelineDeps) -> None:
        process_task = asyncio.create_task(process_job(job_id, deps))
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(job_id, deps, process_task, lease_lost)
        )
        try:
            await process_task
        except asyncio.CancelledError:
            if lease_lost.is_set():
                logger.warning("job %s processing cancelled (lease lost)", job_id)
            else:
                # Cancellation came from outside (pool shutdown, not a lost
                # lease) — must propagate so the enclosing worker-loop task
                # actually finishes instead of looping forever.
                logger.warning("job %s processing cancelled (pool shutting down)", job_id)
                raise
        except Exception:
            logger.exception("job %s failed unexpectedly", job_id)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(
        self,
        job_id: int,
        deps: PipelineDeps,
        process_task: asyncio.Task,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = self.lease_timeout_s / 3
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(_do_heartbeat, deps, job_id)
            except LeaseLost:
                logger.warning("lease lost for job %s; cancelling processing", job_id)
                lease_lost.set()
                process_task.cancel()
                return

    # -- reaper / discovery -----------------------------------------------

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_S)
            with self._session_factory() as session:
                await asyncio.to_thread(jobs.reap_stale, session, self.lease_timeout_s)

    async def _trash_sweep_loop(self) -> None:
        # Runs immediately (first sweep at startup), then hourly — see the
        # module docstring's "Trash sweep" note for why this differs from
        # `_reaper_loop`'s sleep-first shape.
        while True:
            with self._session_factory() as session:
                await asyncio.to_thread(trash.sweep_expired, session)
            await asyncio.sleep(TRASH_SWEEP_INTERVAL_S)

    async def _discovery_loop(self, name: str, discoverer: Discoverer, interval_s: float) -> None:
        while True:
            try:
                await discoverer.poll_once()
            except Exception:
                logger.exception("discovery poll_once failed for instance %s", name)
            await asyncio.sleep(interval_s)
