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

from impostarr import __version__, jobs, trash
from impostarr.jobs import InvalidTransition
from impostarr.models import (
    JOB_STATUSES,
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
from impostarr.plugins.subtitles import parse_srt
from impostarr.remediate import Remediator
from impostarr.trash import RestoreConflict

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
    # Retargeting: null the persisted cursor before this step (start over),
    # and/or start at `series_id` (skipping earlier series) instead of
    # wherever the cursor left off. See Discoverer.backfill_step.
    reset: bool = False
    series_id: int | None = None


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
        trash_count = session.execute(
            select(func.count(TrashItem.id)).where(TrashItem.deleted_at.is_(None))
        ).scalar_one()

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
    refsub_service = getattr(request.app.state, "refsub_service", None)
    return {
        "instances": instances_out,
        "queues": queues,
        "summary": summary,
        "system": system,
        "approval_required": request.app.state.settings.approval_required,
        "active_jobs": active_jobs,
        "workers": {"pool_size": request.app.state.pool_size},
        "dry_run": request.app.state.settings.dry_run,
        "trash_count": trash_count,
        "paused": request.app.state.settings.throttle.paused,
        "refsubs_quota": refsub_service.quota_status() if refsub_service is not None else None,
    }


# -- throttle: pause / resume ------------------------------------------------


@router.post("/pause")
def pause(request: Request) -> dict:
    """Flips the runtime-only pause flag (see `config.ThrottleConfig.paused`
    and `worker.py`'s `_worker_loop`) -- NOT persisted to the config file,
    so it resets to the config's startup value on restart."""
    request.app.state.settings.throttle.paused = True
    return {"paused": True}


@router.post("/resume")
def resume(request: Request) -> dict:
    request.app.state.settings.throttle.paused = False
    return {"paused": False}


# -- queues / job detail --------------------------------------------------


# Latest-verdict s_claimed per job, as a correlated scalar subquery rather
# than a JOIN: a job can have multiple verdicts (human re-verdicts), and a
# JOIN restricted to "the latest one" would need this same subquery in its
# ON clause anyway — the scalar-subquery form sidesteps any risk of
# duplicate-row fan-out and is portable to both SQLite and Postgres.
_LATEST_VERDICT_S_CLAIMED = (
    select(Verdict.s_claimed).where(Verdict.job_id == Job.id).order_by(Verdict.id.desc()).limit(1).correlate(Job).scalar_subquery()
)

QUEUE_SORT_FIELDS = {
    "updated_at": Job.updated_at,
    "created_at": Job.created_at,
    "confidence": _LATEST_VERDICT_S_CLAIMED,
    "series": File.series_id,
    "instance": Instance.name,
}


@router.get("/queues/{status}")
def get_queue(
    status: str,
    request: Request,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    instance: str | None = None,
    sort: Literal["updated_at", "created_at", "confidence", "series", "instance"] = "updated_at",
    dir: Literal["asc", "desc"] = "desc",
) -> dict:
    if status not in JOB_STATUSES:
        raise HTTPException(400, f"invalid status: {status!r}")
    page_size = min(page_size, MAX_PAGE_SIZE)
    sort_column = QUEUE_SORT_FIELDS[sort]
    # nulls_last unconditionally: a no-op for columns that are never null
    # (updated_at/created_at/series/instance), correct for confidence (a job
    # with no verdict yet, or a null s_claimed, sorts after every scored one).
    order = sort_column.asc().nulls_last() if dir == "asc" else sort_column.desc().nulls_last()
    # Job.id tiebreak, same direction as the primary sort: `sort_column`
    # alone is not guaranteed unique (many jobs can share the same
    # updated_at/created_at/confidence/series/instance value), so without a
    # deterministic final key, ties are ordered however the DB engine
    # happens to visit matching rows for a given query plan — an
    # implementation detail, NOT a documented guarantee, that can differ
    # between two otherwise-identical requests (e.g. an unrelated row's
    # write nudging the query plan or physical layout between the initial
    # queue fetch and a debounced SSE-triggered refetch of the same page).
    # When that happens, a row's on-screen *position* can silently point at
    # a different job than it did a moment before, with no visible reorder
    # — this is the root cause of a click landing on the wrong job's modal
    # (P0: "clicked E20's row, got E01's job"). Root-caused via `git log`/
    # code review, not observed failure — SQLite's plain-scan tie order is
    # incidentally stable in this test suite, but the missing tiebreak is a
    # real correctness gap, more readily observable on Postgres (different
    # scan/plan strategies) and under real concurrent writes. Tying every
    # sort field to a final deterministic key removes the non-determinism
    # regardless of backend.
    tiebreak = Job.id.asc() if dir == "asc" else Job.id.desc()
    with _session_factory(request)() as session:
        base_where = [Job.status == status]
        joins: list = []
        # File is needed whenever filtering or sorting touches it or Instance
        # (Instance is only reachable via File.instance_id); Instance itself
        # only when actually filtering/sorting by instance name.
        needs_file = instance is not None or sort in ("series", "instance")
        needs_instance = instance is not None or sort == "instance"
        if needs_file:
            joins.append((File, Job.file_id == File.id))
        if needs_instance:
            joins.append((Instance, File.instance_id == Instance.id))
        if instance is not None:
            base_where.append(Instance.name == instance)

        count_query = select(func.count(Job.id))
        job_query = select(Job)
        for target, on_clause in joins:
            count_query = count_query.join(target, on_clause)
            job_query = job_query.join(target, on_clause)
        count_query = count_query.where(*base_where)
        job_query = (
            job_query.where(*base_where)
            .order_by(order, tiebreak)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

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


async def _series_external_ids(request: Request, file: File) -> dict[str, Any] | None:
    """Best-effort live lookup of the claimed series' cross-database ids
    (for the inspect modal's TVDB/IMDB links + title), via the same
    per-instance runtime `_instance_runtime_for_file` resolves for approve.
    Broad except is deliberate: this is a non-critical enrichment of the
    job-detail response — an unreachable/misconfigured instance, or the
    file's instance having no runtime, should degrade to `None`, not fail
    the whole job-detail request.

    `sonarr_url` is this instance's own series page (base URL + title
    slug) — the only reliable deep link this data supports. A true
    per-*episode* TVDB/IMDB or Sonarr URL isn't constructible from what we
    have: Sonarr's Episode model here carries no per-episode TVDB id (only
    the series-level one above), so a dupe-info "other file" link or a
    plugin-results-table episode deep link falls back to this series page
    rather than a fabricated episode URL — see InspectModal's
    `episodeDeepLink` comment for where that fallback is applied."""
    try:
        runtime = _instance_runtime_for_file(request, file)
        series = await runtime.client.series(file.series_id)
    except Exception:
        logger.warning("series lookup failed for external_ids (series_id=%s)", file.series_id, exc_info=True)
        return None
    return {
        "title": series.title,
        "tvdb_id": series.tvdb_id,
        "imdb_id": series.imdb_id,
        "tmdb_id": series.tmdb_id,
        "sonarr_url": f"{runtime.cfg.url.rstrip('/')}/series/{series.title_slug}",
    }


def _referenced_episode_ids(
    file: File, verdict: Verdict | None, plugin_results: list[PluginResultRow]
) -> set[int]:
    """Every Sonarr episode id referenced anywhere in a job-detail payload:
    the file's own claimed episode(s), a proposed remap's target(s), and
    every in-series candidate any plugin normalized against — the inputs
    `_episode_labels` resolves to `{id, season, episode, title}` so the UI
    never has to render a bare episode id (P0.5)."""
    ids: set[int] = set(file.episode_ids)
    if verdict is not None and isinstance(verdict.proposed_action, dict):
        targets = verdict.proposed_action.get("target_episode_ids")
        if isinstance(targets, list):
            ids.update(i for i in targets if isinstance(i, int))
    for pr in plugin_results:
        if not isinstance(pr.normalized, list):
            continue
        for entry in pr.normalized:
            if isinstance(entry, dict) and entry.get("kind") == "in_series":
                episode_ids = entry.get("episode_ids")
                if isinstance(episode_ids, list):
                    ids.update(i for i in episode_ids if isinstance(i, int))
    return ids


async def _episode_labels(request: Request, file: File, target_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Best-effort `{episode_id: {id, season, episode, title}}` for every id
    in `target_ids`, via the same per-instance Sonarr episode lookup
    `_resolve_human_ident_ids` uses. Degrades to `{}` on any failure (same
    broad-except rationale as `_series_external_ids`) — a raw id is still
    better than a broken job-detail request, though the frontend prefers
    the resolved label whenever one is present."""
    if not target_ids:
        return {}
    try:
        runtime = _instance_runtime_for_file(request, file)
        episodes = await runtime.client.episodes(file.series_id)
    except Exception:
        logger.warning("episode lookup failed for episode_labels (series_id=%s)", file.series_id, exc_info=True)
        return {}
    return {
        ep.id: {"id": ep.id, "season": ep.season_number, "episode": ep.episode_number, "title": ep.title}
        for ep in episodes
        if ep.id in target_ids
    }


def _embedded_subs_payload(asset: Asset) -> dict[str, Any] | None:
    """Best-effort embedded-subtitle text for the three-way text comparison's
    left column, for a text-coded `subs` asset (`.srt` on disk — image-sub
    codecs like PGS/VobSub have no text to show here and are skipped).
    Read from disk rather than stored in the DB: `extract_embedded_subs`
    persists only a path (see `assets/extract.py`), matching how frame
    assets work — this mirrors what `get_job_asset` already does for raw
    bytes, just parsed to cue text instead of streamed. Missing/unreadable
    files degrade to `None`, same non-fatal-enrichment rationale as
    `_series_external_ids`."""
    if asset.type != "subs" or asset.path is None or not asset.path.endswith(".srt"):
        return None
    try:
        text = Path(asset.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    language = (asset.tool_meta or {}).get("language") if isinstance(asset.tool_meta, dict) else None
    return {"cues": parse_srt(text), "language": language}


def _reference_subtitles(plugin_results: list[PluginResultRow]) -> list[dict[str, Any]]:
    """Reference-subtitle text for the three-way comparison's right column,
    sourced from whisper-subs candidates' `evidence.refsub_path` (the only
    plugin that fetches reference subs — see
    `impostarr_plugin_whisper_subs`). Deduplicated by path (the same
    reference file backs one candidate per nearby episode) and read
    best-effort — a cache eviction or path this instance doesn't have
    mounted degrades that one entry to being omitted, not a failed
    request."""
    seen_paths: set[str] = set()
    tracks: list[dict[str, Any]] = []
    for pr in plugin_results:
        if not isinstance(pr.candidates, list):
            continue
        for candidate in pr.candidates:
            if not isinstance(candidate, dict):
                continue
            evidence = candidate.get("evidence")
            if not isinstance(evidence, dict):
                continue
            path = evidence.get("refsub_path")
            if not isinstance(path, str) or path in seen_paths:
                continue
            seen_paths.add(path)
            ident = candidate.get("ident")
            label = "reference subtitle"
            if isinstance(ident, dict) and isinstance(ident.get("season"), int) and isinstance(ident.get("episodes"), list):
                season = ident["season"]
                episodes = "".join(f"E{ep:02d}" for ep in ident["episodes"] if isinstance(ep, int))
                label = f"S{season:02d}{episodes}"
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tracks.append({"label": label, "language": evidence.get("refsub_language"), "cues": parse_srt(text)})
    return tracks


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: int, request: Request) -> dict:
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
        corpus_entry = (
            session.execute(
                select(PhashCorpusEntry).where(PhashCorpusEntry.frame_hash_id == frame_hash.id)
            ).scalars().first()
            if frame_hash is not None
            else None
        )
        external_ids = await _series_external_ids(request, file)
        episode_labels = await _episode_labels(
            request, file, _referenced_episode_ids(file, verdict, plugin_results)
        )
        reference_subtitles = _reference_subtitles(plugin_results)
        return {
            "job": {
                "id": job.id,
                "status": job.status,
                "attempts": job.attempts,
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
            },
            "instance": instance_row.name if instance_row is not None else None,
            "external_ids": external_ids,
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
                    "payload": (
                        a.payload
                        if a.type in ("probe", "transcript")
                        else _embedded_subs_payload(a) if a.type == "subs" else None
                    ),
                }
                for a in assets
            ],
            "episode_labels": episode_labels,
            "reference_subtitles": reference_subtitles,
            "frame_hash_present": frame_hash is not None,
            "frame_hash": (
                {"algo": frame_hash.algo, "version": frame_hash.version, "n_frames": len(frame_hash.hashes)}
                if frame_hash is not None
                else None
            ),
            "phash_corpus": (
                {"confidence": corpus_entry.confidence, "source": corpus_entry.source}
                if corpus_entry is not None
                else None
            ),
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


@router.get("/jobs/{job_id}/datapack")
def get_job_datapack(job_id: int, request: Request) -> Response:
    """Attachment-download JSON bundle for filing an issue against a job:
    the job/file rows (paths included — that's the point, see the UI's
    "include file paths" note), every verdict ever recorded for it (not
    just the latest — the full history), all plugin_results, asset
    metadata (paths/tool_meta, never raw bytes), a log excerpt spanning the
    job's own created_at..updated_at window, and the running app version.

    Nothing here is redacted based on any request parameter: the frontend's
    "include file paths" checkbox only gates whether its Download button is
    enabled (a 2-click confirmation that this file contains local paths),
    it does not change what this endpoint returns — see UI-SPEC section 7.
    """
    with _session_factory(request)() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        file = session.get(File, job.file_id)
        verdicts = (
            session.execute(select(Verdict).where(Verdict.job_id == job_id).order_by(Verdict.id.asc()))
            .scalars()
            .all()
        )
        plugin_results = (
            session.execute(select(PluginResultRow).where(PluginResultRow.job_id == job_id)).scalars().all()
        )
        assets = session.execute(select(Asset).where(Asset.file_id == job.file_id)).scalars().all()

        window_start = job.created_at.isoformat()
        window_end = job.updated_at.isoformat()
        all_logs = request.app.state.log_buffer.get_logs(limit=request.app.state.log_buffer.capacity)
        log_excerpt = [record for record in all_logs if window_start <= record["ts"] <= window_end]

        bundle = {
            "app_version": __version__,
            "generated_at": datetime.now(UTC).isoformat(),
            "job": {
                "id": job.id,
                "status": job.status,
                "attempts": job.attempts,
                "claimed_by": job.claimed_by,
                "claimed_at": _iso(job.claimed_at),
                "heartbeat_at": _iso(job.heartbeat_at),
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
            },
            "file": (
                {
                    "id": file.id,
                    "instance_id": file.instance_id,
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
                }
                if file is not None
                else None
            ),
            "verdicts": [
                {
                    "id": v.id,
                    "s_claimed": v.s_claimed,
                    "s_alt": v.s_alt,
                    "outcome": v.outcome,
                    "proposed_action": v.proposed_action,
                    "remediation_log": v.remediation_log,
                    "source": v.source,
                    "human_ident": v.human_ident,
                    "dupe_info": v.dupe_info,
                    "created_at": v.created_at.isoformat(),
                }
                for v in verdicts
            ],
            "plugin_results": [
                {
                    "name": pr.plugin_name,
                    "version": pr.plugin_version,
                    "status": pr.status,
                    "reason": pr.reason,
                    "candidates": pr.candidates,
                    "normalized": pr.normalized,
                    "created_at": pr.created_at.isoformat(),
                }
                for pr in plugin_results
            ],
            "assets": [
                {"id": a.id, "type": a.type, "path": a.path, "tool_meta": a.tool_meta, "created_at": a.created_at.isoformat()}
                for a in assets
            ],
            "log_excerpt": log_excerpt,
        }

    body = json.dumps(bundle, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="impostarr-job-{job_id}-datapack.json"'},
    )


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
    remediator = Remediator(
        runtime.client, runtime.cfg, session_factory,
        dry_run=dry_run, trash_cfg=request.app.state.settings.trash,
    )
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
    result = await runtime.discoverer.backfill_step(
        body.batch_size, reset=body.reset, series_id=body.series_id
    )
    return {"created": result.created, "skipped": result.skipped}


# -- trash --------------------------------------------------------------


def _trash_item_out(item: TrashItem, now: datetime) -> dict:
    return {
        "id": item.id,
        "instance": item.instance,
        "original_path": item.original_path,
        "trash_path": item.trash_path,
        "series_id": item.series_id,
        "episode_ids": item.episode_ids,
        "size": item.size,
        "trashed_at": _iso(item.trashed_at),
        "expires_at": _iso(item.expires_at),
        "expires_in_s": (item.expires_at - now).total_seconds(),
    }


@router.get("/trash")
def list_trash(request: Request, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    page_size = min(page_size, MAX_PAGE_SIZE)
    with _session_factory(request)() as session:
        base_where = [TrashItem.deleted_at.is_(None)]
        total = session.execute(
            select(func.count(TrashItem.id)).where(*base_where)
        ).scalar_one()
        items = (
            session.execute(
                select(TrashItem)
                .where(*base_where)
                .order_by(TrashItem.trashed_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        return {
            "total": total,
            "page_size": page_size,
            "items": [_trash_item_out(item, now) for item in items],
        }


def _get_active_trash_item(session, trash_id: int) -> TrashItem:
    item = session.get(TrashItem, trash_id)
    if item is None:
        raise HTTPException(404, "trash item not found")
    if item.deleted_at is not None:
        raise HTTPException(409, f"trash item {trash_id} already {item.outcome}")
    return item


@router.delete("/trash/{trash_id}")
def delete_trash_item(trash_id: int, request: Request) -> dict:
    with _session_factory(request)() as session:
        item = _get_active_trash_item(session, trash_id)
        trash.delete_now(session, item)
        return {"result": "deleted"}


@router.post("/trash/{trash_id}/restore")
def restore_trash_item(trash_id: int, request: Request) -> dict:
    with _session_factory(request)() as session:
        item = _get_active_trash_item(session, trash_id)
        try:
            trash.restore(session, item)
        except RestoreConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "result": "restored",
            "original_path": item.original_path,
            "note": "not re-imported into Sonarr; a manual rescan/import is required",
        }


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
