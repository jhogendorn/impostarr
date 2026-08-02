"""Async worker pool: orchestration only. Claim/heartbeat/process loop,
lease reaper, and per-instance discovery scheduling all live here as thin
glue; the actual pipeline logic is in `pipeline.py` and the queue mechanics
are in `jobs.py`.

Worker identity: each `PipelineDeps` carries a fixed `worker_id` for its
instance. All worker-loop tasks that claim/heartbeat/release jobs for that
instance share that one `worker_id` (it is not per-asyncio-task) — the
`jobs.py` lease fencing only needs `worker_id` to be *this pool's* identity
for a job, not unique per concurrent coroutine; `claim_next`'s atomic UPDATE
already guarantees two coroutines can never claim the same job regardless of
whether they pass the same `worker_id` string.
"""

from __future__ import annotations

import asyncio
import logging

from impostarr import jobs
from impostarr.discovery import Discoverer
from impostarr.jobs import LeaseLost
from impostarr.models import Job
from impostarr.pipeline import PipelineDeps, process_job

logger = logging.getLogger(__name__)

POLL_EMPTY_SLEEP_S = 5.0
REAP_INTERVAL_S = 60.0


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
        self._tasks = [asyncio.create_task(self._worker_loop()) for _ in range(self.pool_size)]
        self._tasks.append(asyncio.create_task(self._reaper_loop()))
        for name, discoverer in self.discoverers.items():
            interval = self.deps_per_instance[name].instance_cfg.poll_interval_s
            self._tasks.append(asyncio.create_task(self._discovery_loop(name, discoverer, interval)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # -- worker loop -----------------------------------------------------

    async def _worker_loop(self) -> None:
        instance_names = list(self.deps_per_instance)
        idx = 0
        while True:
            if not instance_names:
                await asyncio.sleep(POLL_EMPTY_SLEEP_S)
                continue
            claimed = False
            for _ in range(len(instance_names)):
                name = instance_names[idx]
                idx = (idx + 1) % len(instance_names)
                deps = self.deps_per_instance[name]
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
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id, deps, process_task))
        try:
            await process_task
        except asyncio.CancelledError:
            logger.warning("job %s processing cancelled (lease lost)", job_id)
        except Exception:
            logger.exception("job %s failed unexpectedly", job_id)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(
        self, job_id: int, deps: PipelineDeps, process_task: asyncio.Task
    ) -> None:
        interval = self.lease_timeout_s / 3
        while True:
            await asyncio.sleep(interval)
            with deps.session_factory() as session:
                job = session.get(Job, job_id)
                try:
                    await asyncio.to_thread(jobs.heartbeat, session, job, deps.worker_id)
                except LeaseLost:
                    logger.warning("lease lost for job %s; cancelling processing", job_id)
                    process_task.cancel()
                    return

    # -- reaper / discovery -----------------------------------------------

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_S)
            with self._session_factory() as session:
                await asyncio.to_thread(jobs.reap_stale, session, self.lease_timeout_s)

    async def _discovery_loop(self, name: str, discoverer: Discoverer, interval_s: float) -> None:
        while True:
            try:
                await discoverer.poll_once()
            except Exception:
                logger.exception("discovery poll_once failed for instance %s", name)
            await asyncio.sleep(interval_s)
