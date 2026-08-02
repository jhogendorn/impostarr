"""REST + SSE routes, all JSON under `/api/v1` (except `/api/v1/events`,
which is `text/event-stream`).

Runtime state (DB session factory, the `EventBus`, and per-instance Sonarr
clients/configs/discoverers) is looked up from `request.app.state`, set up
by `main.create_app` — this module has no composition-root concerns of its
own, only request handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

from impostarr import jobs
from impostarr.jobs import InvalidTransition
from impostarr.models import (
    JOB_STATUSES,
    Asset,
    File,
    FrameHash,
    Instance,
    Job,
    PhashCorpusEntry,
    Verdict,
)
from impostarr.models import PluginResult as PluginResultRow
from impostarr.remediate import Remediator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
STATS_INTERVAL_S = 15.0
# Human verdicts only apply to jobs actually awaiting human review (spec:
# "Human verdicts from quarantine/inconclusive"). Allowlist, not a
# blocklist of active/pending: hold/matched/error/remediated must also be
# rejected — a blocklist that only named active/pending let `hold` jobs
# through, producing orphaned verdicts and evidence-free quarantine shells.
VERDICT_ALLOWED_STATUSES = frozenset({"quarantine", "inconclusive"})

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".wav": "audio/wav",
}


# -- request bodies ---------------------------------------------------------


class HumanIdent(BaseModel):
    season: int
    episodes: list[int]


class VerdictRequest(BaseModel):
    verdict: Literal["is_claimed", "is_other", "ignore"]
    ident: HumanIdent | None = None


class BackfillRequest(BaseModel):
    batch_size: int = 100


# -- shared helpers -----------------------------------------------------


def _session_factory(request: Request):
    return request.app.state.session_factory


def _publish(request: Request, job_id: int, status: str) -> None:
    bus = request.app.state.event_bus
    if bus is not None:
        bus.publish({"type": "job_update", "job_id": job_id, "status": status})


def _latest_verdict(session, job_id: int) -> Verdict | None:
    return (
        session.execute(select(Verdict).where(Verdict.job_id == job_id).order_by(Verdict.id.desc()))
        .scalars()
        .first()
    )


def _queue_counts(session) -> dict[str, int]:
    counts = {status: 0 for status in JOB_STATUSES}
    for status, count in session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)):
        counts[status] = count
    return counts


def _instance_names_by_id(session) -> dict[int, str]:
    return dict(session.execute(select(Instance.id, Instance.name)).all())


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _instance_runtime_for_file(request: Request, file: File):
    with _session_factory(request)() as session:
        instance = session.get(Instance, file.instance_id)
    runtime = request.app.state.instances.get(instance.name)
    if runtime is None:
        raise HTTPException(500, f"no runtime configured for instance {instance.name!r}")
    return runtime


async def _resolve_human_ident_ids(request: Request, file: File, ident: HumanIdent) -> set[int] | None:
    """Map a human-entered (season, episodes) ident to Sonarr episode ids,
    tvdb-standard-numbering only (what the human verdict UI collects)."""
    runtime = _instance_runtime_for_file(request, file)
    episodes = await runtime.client.episodes(file.series_id)
    ids: set[int] = set()
    for number in ident.episodes:
        match = next(
            (ep for ep in episodes if ep.season_number == ident.season and ep.episode_number == number),
            None,
        )
        if match is None:
            return None
        ids.add(match.id)
    return ids


# -- healthz / status -----------------------------------------------------


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/logs")
def get_logs(request: Request, level: str | None = None, limit: int = 200) -> dict:
    """Recent records from the in-memory ring buffer (Task: log viewer),
    newest last. `level` filters at-or-above (INFO/WARNING/ERROR/...);
    omitted returns every buffered level."""
    buffer = request.app.state.log_buffer
    return {"items": buffer.get_logs(level=level, limit=limit)}


@router.get("/status")
def get_status(request: Request) -> dict:
    with _session_factory(request)() as session:
        instances = session.execute(select(Instance)).scalars().all()
        instances_out = [
            {
                "name": i.name,
                "url": i.url,
                "history_watermark": i.history_watermark,
                "backfill_cursor": i.backfill_cursor,
                "last_polled_at": _iso(i.last_polled_at),
                "last_backfilled_at": _iso(i.last_backfilled_at),
            }
            for i in instances
        ]
        queues = _queue_counts(session)

        instance_names = _instance_names_by_id(session)
        active_jobs = []
        active_job_rows = session.execute(select(Job).where(Job.status == "active")).scalars().all()
        now = datetime.now(UTC)
        for job in active_job_rows:
            file = session.get(File, job.file_id)
            active_jobs.append(
                {
                    "job_id": job.id,
                    "instance": instance_names.get(file.instance_id) if file else None,
                    "series_id": file.series_id if file else None,
                    "sonarr_path": file.sonarr_path if file else None,
                    "claimed_by": job.claimed_by,
                    "claimed_at": _iso(job.claimed_at),
                    "elapsed_s": (now - job.claimed_at).total_seconds() if job.claimed_at else None,
                }
            )

    summary = {
        "unprocessed": queues["hold"] + queues["pending"] + queues["active"],
        "processed": (
            queues["matched"]
            + queues["quarantine"]
            + queues["inconclusive"]
            + queues["error"]
            + queues["remediated"]
        ),
    }
    system = {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": psutil.virtual_memory().percent,
    }
    return {
        "instances": instances_out,
        "queues": queues,
        "summary": summary,
        "system": system,
        "approval_required": request.app.state.settings.approval_required,
        "active_jobs": active_jobs,
        "workers": {"pool_size": request.app.state.pool_size},
        "dry_run": request.app.state.settings.dry_run,
    }


# -- queues / job detail --------------------------------------------------


QUEUE_SORT_FIELDS = {"updated_at": Job.updated_at, "created_at": Job.created_at}


@router.get("/queues/{status}")
def get_queue(
    status: str,
    request: Request,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    instance: str | None = None,
    sort: Literal["updated_at", "created_at"] = "updated_at",
    dir: Literal["asc", "desc"] = "desc",
) -> dict:
    if status not in JOB_STATUSES:
        raise HTTPException(400, f"invalid status: {status!r}")
    page_size = min(page_size, MAX_PAGE_SIZE)
    sort_column = QUEUE_SORT_FIELDS[sort]
    order = sort_column.asc() if dir == "asc" else sort_column.desc()
    with _session_factory(request)() as session:
        base_where = [Job.status == status]
        joins: list = []
        if instance is not None:
            joins = [(File, Job.file_id == File.id), (Instance, File.instance_id == Instance.id)]
            base_where.append(Instance.name == instance)

        count_query = select(func.count(Job.id))
        job_query = select(Job)
        for target, on_clause in joins:
            count_query = count_query.join(target, on_clause)
            job_query = job_query.join(target, on_clause)
        count_query = count_query.where(*base_where)
        job_query = job_query.where(*base_where).order_by(order).offset((page - 1) * page_size).limit(page_size)

        total = session.execute(count_query).scalar_one()
        job_rows = session.execute(job_query).scalars().all()

        instance_names = _instance_names_by_id(session)
        items = []
        for job in job_rows:
            file = session.get(File, job.file_id)
            verdict = _latest_verdict(session, job.id)
            items.append(
                {
                    "job_id": job.id,
                    "status": job.status,
                    "instance": instance_names.get(file.instance_id) if file else None,
                    "file": {
                        "series_id": file.series_id,
                        "sonarr_path": file.sonarr_path,
                        "episode_ids": file.episode_ids,
                    },
                    "verdict": (
                        {"s_claimed": verdict.s_claimed, "s_alt": verdict.s_alt, "outcome": verdict.outcome}
                        if verdict is not None
                        else None
                    ),
                    "created_at": job.created_at.isoformat(),
                    "updated_at": job.updated_at.isoformat(),
                }
            )
    return {"total": total, "page_size": page_size, "items": items}


@router.get("/jobs/{job_id}")
def get_job_detail(job_id: int, request: Request) -> dict:
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        file = session.get(File, job.file_id)
        plugin_results = (
            session.execute(select(PluginResultRow).where(PluginResultRow.job_id == job_id)).scalars().all()
        )
        verdict = _latest_verdict(session, job_id)
        assets = session.execute(select(Asset).where(Asset.file_id == file.id)).scalars().all()
        frame_hash = (
            session.execute(select(FrameHash).where(FrameHash.file_id == file.id).order_by(FrameHash.id.desc()))
            .scalars()
            .first()
        )
        instance_row = session.get(Instance, file.instance_id)
        return {
            "job": {
                "id": job.id,
                "status": job.status,
                "attempts": job.attempts,
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
            },
            "instance": instance_row.name if instance_row is not None else None,
            "file": {
                "series_id": file.series_id,
                "episode_ids": file.episode_ids,
                "episode_file_id": file.episode_file_id,
                "sonarr_path": file.sonarr_path,
                "local_path": file.local_path,
                "size": file.size,
                "content_hash": file.content_hash,
                "quality": file.quality,
                "languages": file.languages,
                "history_id": file.history_id,
                "download_id": file.download_id,
                "source_title": file.source_title,
                "indexer": file.indexer,
                "guid": file.guid,
            },
            "plugin_results": [
                {
                    "name": pr.plugin_name,
                    "version": pr.plugin_version,
                    "status": pr.status,
                    "reason": pr.reason,
                    "candidates": pr.candidates,
                    "normalized": pr.normalized,
                }
                for pr in plugin_results
            ],
            "verdict": (
                {
                    "s_claimed": verdict.s_claimed,
                    "s_alt": verdict.s_alt,
                    "outcome": verdict.outcome,
                    "proposed_action": verdict.proposed_action,
                    "remediation_log": verdict.remediation_log,
                    "source": verdict.source,
                    "human_ident": verdict.human_ident,
                    "dupe_info": verdict.dupe_info,
                }
                if verdict is not None
                else None
            ),
            "assets": [
                {
                    "id": a.id,
                    "type": a.type,
                    "path": a.path,
                    "has_path": a.path is not None,
                    "tool_meta": a.tool_meta,
                    "payload": a.payload if a.type in ("probe", "transcript") else None,
                }
                for a in assets
            ],
            "frame_hash_present": frame_hash is not None,
        }


@router.get("/jobs/{job_id}/assets/{asset_id}")
def get_job_asset(job_id: int, asset_id: int, request: Request) -> Response:
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        asset = session.get(Asset, asset_id)
        if asset is None or asset.file_id != job.file_id:
            raise HTTPException(404, "asset not found")
        if asset.path is not None:
            path = Path(asset.path)
            if not path.exists():
                raise HTTPException(404, "asset file missing on disk")
            media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
            return FileResponse(path, media_type=media_type)
        if asset.payload is not None:
            return JSONResponse(asset.payload)
        raise HTTPException(404, "asset has no content")


# -- verdict / approve / reject --------------------------------------------


def _latest_frame_hash(session, file_id: int) -> FrameHash | None:
    return (
        session.execute(select(FrameHash).where(FrameHash.file_id == file_id).order_by(FrameHash.id.desc()))
        .scalars()
        .first()
    )


def _write_phash_corpus_for_verdict(
    session, file: File, frame_hash: FrameHash, episodes: list, confidence: float, source: str
) -> None:
    # external_ids is left empty here (unlike pipeline.py's auto corpus
    # write, which has series context on hand already): fetching
    # client.series() just for tvdb/tmdb/imdb ids would be a second Sonarr
    # call on every human is_claimed verdict for comparatively low value —
    # season/episode numbers (the corpus lookup key) are still populated.
    ids = set(file.episode_ids)
    matched = [ep for ep in episodes if ep.id in ids]
    if not matched:
        logger.warning("phash corpus write: no episodes matched ids %s", sorted(ids))
        return
    season = matched[0].season_number
    numbers = sorted(ep.episode_number for ep in matched)
    entry = PhashCorpusEntry(
        frame_hash_id=frame_hash.id,
        external_ids={},
        season=season,
        episodes=numbers,
        confidence=confidence,
        source=source,
    )
    session.add(entry)
    session.commit()


@router.post("/jobs/{job_id}/verdict")
async def post_verdict(job_id: int, body: VerdictRequest, request: Request) -> dict:
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        expected_current = job.status
        if expected_current not in VERDICT_ALLOWED_STATUSES:
            raise HTTPException(
                409, f"job is {expected_current!r}; verdicts only apply to quarantine/inconclusive jobs"
            )
        if body.verdict == "is_other" and body.ident is None:
            raise HTTPException(400, "ident is required for is_other")
        file = session.get(File, job.file_id)

        # Fence the status transition FIRST, before writing the verdict row:
        # if a concurrent verdict submission already moved the job off
        # `expected_current`, this raises InvalidTransition (-> 409) and no
        # verdict row is written at all — a losing race leaves exactly the
        # winner's single verdict row, not an orphaned extra one.
        target_status = {"is_claimed": "matched", "is_other": "quarantine", "ignore": "inconclusive"}[
            body.verdict
        ]
        try:
            jobs.set_status_checked(session, job, target_status, expected_current)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

        proposed_remap: dict[str, Any] | None = None

        if body.verdict == "is_claimed":
            verdict = Verdict(job_id=job_id, outcome="matched", source="human", s_claimed=1.0)
            session.add(verdict)
            session.commit()
            frame_hash = _latest_frame_hash(session, file.id)
            if frame_hash is not None:
                runtime = _instance_runtime_for_file(request, file)
                episodes = await runtime.client.episodes(file.series_id)
                _write_phash_corpus_for_verdict(session, file, frame_hash, episodes, 1.0, "human")

        elif body.verdict == "is_other":
            verdict = Verdict(
                job_id=job_id,
                outcome="quarantine",
                source="human",
                human_ident=body.ident.model_dump(),
            )
            session.add(verdict)
            session.commit()
            target_ids = await _resolve_human_ident_ids(request, file, body.ident)
            if target_ids is not None:
                proposed_remap = {"kind": "remap", "target_episode_ids": sorted(target_ids)}

        else:  # ignore
            verdict = Verdict(job_id=job_id, outcome="inconclusive", source="human")
            session.add(verdict)
            session.commit()

        result_status = job.status

    _publish(request, job_id, result_status)
    return {"job_status": result_status, "verdict_id": verdict.id, "proposed_remap": proposed_remap}


@router.post("/jobs/{job_id}/approve")
async def approve_job(job_id: int, request: Request) -> dict:
    identity = getattr(request.state, "identity", "anon")
    session_factory = _session_factory(request)

    with session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status != "quarantine":
            raise HTTPException(409, f"job is {job.status!r}, not quarantine")
        file = session.get(File, job.file_id)
        verdict = _latest_verdict(session, job_id)

        action: dict[str, Any] | None = None
        if verdict is not None and verdict.proposed_action is not None:
            action = verdict.proposed_action
        elif verdict is not None and verdict.source == "human" and verdict.human_ident is not None:
            ident = HumanIdent.model_validate(verdict.human_ident)
            target_ids = await _resolve_human_ident_ids(request, file, ident)
            if target_ids is None:
                raise HTTPException(409, "human ident does not resolve to known episodes")
            action = {"kind": "remap", "target_episode_ids": sorted(target_ids)}

        if action is None:
            raise HTTPException(409, "no proposed action or human ident to approve")

        worker_id = f"api-{identity}"
        try:
            job = jobs.claim_for_api(session, job, worker_id)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    runtime = _instance_runtime_for_file(request, file)
    dry_run = request.app.state.settings.dry_run
    remediator = Remediator(runtime.client, runtime.cfg, session_factory, dry_run=dry_run)
    if action["kind"] == "remap":
        await remediator.remap(job, frozenset(action["target_episode_ids"]), worker_id)
    else:
        await remediator.replace(job, worker_id)

    with session_factory() as session:
        result_status = session.get(Job, job_id).status

    _publish(request, job_id, result_status)
    return {"result": result_status}


@router.post("/jobs/{job_id}/reject")
def reject_job(job_id: int, request: Request) -> dict:
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status != "quarantine":
            raise HTTPException(409, f"job is {job.status!r}, not quarantine")
        verdict = _latest_verdict(session, job_id)
        if verdict is not None:
            verdict.proposed_action = None
            session.commit()
    _publish(request, job_id, "quarantine")
    return {"result": "quarantine"}


# -- park / unpark / rerun --------------------------------------------------


def _transition_endpoint(job_id: int, request: Request, fn) -> dict:
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        try:
            fn(session, job)
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        result_status = job.status
    _publish(request, job_id, result_status)
    return {"result": result_status}


@router.post("/jobs/{job_id}/park")
def park_job(job_id: int, request: Request) -> dict:
    return _transition_endpoint(job_id, request, jobs.park)


@router.post("/jobs/{job_id}/unpark")
def unpark_job(job_id: int, request: Request) -> dict:
    return _transition_endpoint(job_id, request, jobs.unpark)


@router.post("/jobs/{job_id}/rerun")
def rerun_job(job_id: int, request: Request) -> dict:
    return _transition_endpoint(job_id, request, jobs.requeue)


# -- backfill ---------------------------------------------------------------


@router.post("/instances/{name}/backfill")
async def backfill_instance(name: str, body: BackfillRequest, request: Request) -> dict:
    runtime = request.app.state.instances.get(name)
    if runtime is None:
        raise HTTPException(404, f"unknown instance: {name!r}")
    created = await runtime.discoverer.backfill_step(body.batch_size)
    return {"created": created}


# -- SSE ----------------------------------------------------------------


async def _event_stream(request: Request):
    bus = request.app.state.event_bus
    session_factory = _session_factory(request)
    subscription = bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(subscription.__anext__(), timeout=STATS_INTERVAL_S)
            except TimeoutError:
                with session_factory() as session:
                    stats = _queue_counts(session)
                yield f"event: stats\ndata: {json.dumps(stats)}\n\n"
                yield ": heartbeat\n\n"
                continue
            except StopAsyncIteration:
                break
            yield f"event: job_update\ndata: {json.dumps(event)}\n\n"
    finally:
        await subscription.aclose()


@router.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    return StreamingResponse(_event_stream(request), media_type="text/event-stream")
