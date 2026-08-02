"""Worker pipeline: `process_job` runs one job through asset extraction,
identifier plugins, scoring/routing, verdict persistence, and remediation.

Stage caching: probe/audio/subs/frames each compute the same
`assets.extract.fingerprint()` the extractor itself would use *before*
running it, so a matching `assets` row (keyed by `(file_id, type,
input_fingerprint)`) short-circuits the actual ffmpeg/ffprobe subprocess.
subs and frames fan out into multiple output files; the expected
fingerprint list is derived from probe data (stream list / duration)
without re-running ffmpeg, and the stage is only considered "cached" when
every expected fingerprint already has a row. Transcript caching is
existence-only (no fingerprint check) per spec: any `transcript` asset row
means "already transcribed for this file", not"unchanged since transcript
was last extracted" — the audio slice is expected to be stable input.

Plugin caching: `plugin_results` rows are looked up by `(job_id,
plugin_name, input_fingerprint)`; the fingerprint folds in plugin
name+version, plugin config, the asset fingerprints the plugin could see,
the claimed ident, and a digest of the series context, so any of those
changing invalidates the cache and re-runs `identify()`.

Dupe detection (spec: "Dupe check"): frame-hash similarity against other
files' stored `frame_hashes` is computed and logged (`log.warning`) when
`hamming_similarity >= 0.9`, but is **not persisted** in this task — the
spec's data model has no column for it (see the plan doc's Task 14 dupe
check note) and adding one would ripple into other tasks' schema
assumptions. TODO(api-task): surface dupe info via the HTTP API/verdict
once that task adds a place to show it.

DB access is synchronous throughout this module — every `session.*` call
runs directly in the coroutine, not dispatched via `asyncio.to_thread`.
Accepted for the PoC: SQLite is single-writer regardless, and at PoC
worker-pool scale the blocked-event-loop window per query is small relative
to the ffmpeg/plugin work each job already does. Revisit (to_thread, or an
async engine) if worker concurrency against Postgres becomes a bottleneck.

Unexpected-exception handling: `process_job`'s body runs under a catch-all
that releases the job to `error` on any exception it doesn't already handle
explicitly (a stage helper bug, a scoring/routing bug, etc.), so a
deterministic bug fails the job immediately instead of leaving it `active`
until the reaper's lease timeout expires. `LeaseLost` and
`asyncio.CancelledError` are re-raised rather than swallowed — both are
the caller's (worker.py's) concern, not a pipeline failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import xxhash
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from impostarr import jobs
from impostarr.api.events import EventBus
from impostarr.assets import extract
from impostarr.assets.transcribe import Transcriber
from impostarr.config import Settings, SonarrInstance
from impostarr.models import Asset, File, FrameHash, Job, PhashCorpusEntry, Verdict
from impostarr.models import PluginResult as PluginResultRow
from impostarr.normalize import normalize
from impostarr.plugins.base import (
    AssetBundle,
    Candidate,
    ClaimedIdent,
    PluginResult,
    SeriesContext,
)
from impostarr.plugins.loader import LoadedPlugin
from impostarr.remediate import Remediator
from impostarr.scoring import InstanceFlags, PluginOutcome, Remap, aggregate, route
from impostarr.sonarr import SonarrClient

logger = logging.getLogger(__name__)

AUDIO_OFFSET_S = 60.0
AUDIO_DURATION_S = 900.0
FRAME_SAMPLE_N = 16
DUPLICATE_SIMILARITY_THRESHOLD = 0.9


@dataclass
class PipelineDeps:
    session_factory: sessionmaker[Session]
    sonarr_client: SonarrClient
    settings: Settings
    instance_cfg: SonarrInstance
    plugins: list[LoadedPlugin]
    transcriber: Transcriber
    refsubs: Any
    worker_id: str
    # In-process series-context cache, one entry per (instance, series_id)
    # for this process's lifetime (no TTL needed for the PoC).
    series_cache: dict[tuple[str, int], SeriesContext] = field(default_factory=dict)
    series_cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Optional: when set, `_publish` notifies SSE subscribers at each
    # terminal point in `process_job` (Task 15). None in existing tests and
    # any caller that doesn't need live updates (default, no behavior change).
    event_bus: EventBus | None = None


def _publish(deps: PipelineDeps, job_id: int, status: str) -> None:
    if deps.event_bus is not None:
        deps.event_bus.publish({"type": "job_update", "job_id": job_id, "status": status})


# -- series context / claimed ident -----------------------------------------


async def _get_series_context(deps: PipelineDeps, series_id: int) -> SeriesContext:
    key = (deps.instance_cfg.name, series_id)
    async with deps.series_cache_lock:
        cached = deps.series_cache.get(key)
        if cached is not None:
            return cached
        series = await deps.sonarr_client.series(series_id)
        episodes = await deps.sonarr_client.episodes(series_id)
        ctx = SeriesContext(
            series=series.model_dump(),
            episodes=[ep.model_dump() for ep in episodes],
            refsubs=deps.refsubs,
        )
        deps.series_cache[key] = ctx
        return ctx


def _season_and_numbers(
    episode_ids: list[int] | frozenset[int], episodes: list[dict[str, Any]]
) -> tuple[int, list[int]] | None:
    ids = set(episode_ids)
    matched = [ep for ep in episodes if ep.get("id") in ids]
    if not matched:
        return None
    season = matched[0]["season_number"]
    numbers = sorted(ep["episode_number"] for ep in matched)
    return season, numbers


def _build_claimed_ident(file: File, episodes: list[dict[str, Any]]) -> ClaimedIdent | None:
    resolved = _season_and_numbers(file.episode_ids, episodes)
    if resolved is None:
        return None
    season, numbers = resolved
    return ClaimedIdent(season=season, episodes=numbers, episode_ids=list(file.episode_ids))


# -- asset stage helpers ------------------------------------------------


def _asset_row_by_fp(session: Session, file_id: int, type_: str, fp: str) -> Asset | None:
    return session.execute(
        select(Asset).where(
            Asset.file_id == file_id, Asset.type == type_, Asset.input_fingerprint == fp
        )
    ).scalar_one_or_none()


def _latest_asset(session: Session, file_id: int, type_: str) -> Asset | None:
    return (
        session.execute(
            select(Asset)
            .where(Asset.file_id == file_id, Asset.type == type_)
            .order_by(Asset.id.desc())
        )
        .scalars()
        .first()
    )


def _latest_frame_hash(session: Session, file_id: int) -> FrameHash | None:
    return (
        session.execute(
            select(FrameHash).where(FrameHash.file_id == file_id).order_by(FrameHash.id.desc())
        )
        .scalars()
        .first()
    )


def _persist_asset_row(
    session: Session, file_id: int, extracted: extract.ExtractedAsset
) -> Asset:
    row = Asset(
        file_id=file_id,
        type=extracted.type,
        path=extracted.path,
        payload=extracted.payload,
        input_fingerprint=extracted.input_fingerprint,
        tool_meta=extracted.tool_meta,
    )
    session.add(row)
    session.commit()
    return row


def _extracted_from_row(row: Asset) -> extract.ExtractedAsset:
    return extract.ExtractedAsset(
        type=row.type,
        path=row.path,
        payload=row.payload,
        input_fingerprint=row.input_fingerprint,
        tool_meta=row.tool_meta,
    )


async def _stage_probe(session: Session, file: File, path: Path) -> extract.ExtractedAsset | None:
    try:
        fp = extract.fingerprint(path, "probe", "")
    except OSError as exc:
        logger.warning("cannot fingerprint %s for probe: %s", path, exc)
        return None
    row = _asset_row_by_fp(session, file.id, "probe", fp)
    if row is not None:
        return _extracted_from_row(row)
    try:
        result = await extract.probe(path)
    except extract.ExtractError as exc:
        logger.warning("probe failed for file %s: %s", file.id, exc)
        return None
    _persist_asset_row(session, file.id, result)
    return result


async def _stage_audio(
    session: Session,
    file: File,
    path: Path,
    out_dir: Path,
    probe_asset: extract.ExtractedAsset | None,
) -> extract.ExtractedAsset | None:
    try:
        params = f"offset={AUDIO_OFFSET_S}:duration={AUDIO_DURATION_S}"
        fp = extract.fingerprint(path, "extract_audio", params)
    except OSError as exc:
        logger.warning("cannot fingerprint %s for audio: %s", path, exc)
        return None
    row = _asset_row_by_fp(session, file.id, "audio", fp)
    if row is not None:
        return _extracted_from_row(row)
    try:
        result = await extract.extract_audio(
            path,
            out_dir,
            offset_s=AUDIO_OFFSET_S,
            duration_s=AUDIO_DURATION_S,
            probe_result=probe_asset,
        )
    except extract.ExtractError as exc:
        logger.warning("audio extraction failed for file %s: %s", file.id, exc)
        return None
    _persist_asset_row(session, file.id, result)
    return result


def _expected_sub_fingerprints(path: Path, probe_payload: dict[str, Any]) -> list[str]:
    streams = probe_payload.get("streams", [])
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    fps = []
    for idx, stream in enumerate(sub_streams):
        codec = stream.get("codec_name")
        if codec not in extract.TEXT_SUB_CODECS and codec not in extract.IMAGE_SUB_CODECS:
            continue
        params = f"stream={idx}:codec={codec}"
        fps.append(extract.fingerprint(path, "extract_embedded_subs", params))
    return fps


async def _stage_subs(
    session: Session,
    file: File,
    path: Path,
    out_dir: Path,
    probe_asset: extract.ExtractedAsset | None,
) -> list[extract.ExtractedAsset] | None:
    if probe_asset is not None:
        try:
            expected_fps = _expected_sub_fingerprints(path, probe_asset.payload or {})
        except OSError:
            expected_fps = None
        if expected_fps is not None:
            if not expected_fps:
                return []
            rows = [_asset_row_by_fp(session, file.id, "subs", fp) for fp in expected_fps]
            if all(r is not None for r in rows):
                return [_extracted_from_row(r) for r in rows]  # type: ignore[arg-type]
    try:
        results = await extract.extract_embedded_subs(path, out_dir, probe_result=probe_asset)
    except extract.ExtractError as exc:
        logger.warning("subs extraction failed for file %s: %s", file.id, exc)
        return None
    for r in results:
        _persist_asset_row(session, file.id, r)
    return results


def _expected_frame_fingerprints(
    path: Path, probe_payload: dict[str, Any], n: int
) -> list[str]:
    total = float(probe_payload["format"]["duration"])
    fps = []
    for i in range(n):
        ts = (i + 0.5) / n * total
        params = f"n={n}:index={i}:ts={ts:.6f}"
        fps.append(extract.fingerprint(path, "sample_frames", params))
    return fps


async def _stage_frames(
    session: Session,
    file: File,
    path: Path,
    out_dir: Path,
    probe_asset: extract.ExtractedAsset | None,
    n: int = FRAME_SAMPLE_N,
) -> tuple[extract.FrameHashSeq | None, FrameHash | None]:
    existing_hash_row = _latest_frame_hash(session, file.id)
    if probe_asset is not None and existing_hash_row is not None:
        try:
            expected_fps = _expected_frame_fingerprints(path, probe_asset.payload or {}, n)
        except (KeyError, TypeError, ValueError, OSError):
            expected_fps = None
        if expected_fps is not None:
            rows = [_asset_row_by_fp(session, file.id, "frames", fp) for fp in expected_fps]
            if rows and all(r is not None for r in rows):
                seq = extract.FrameHashSeq(
                    algo=existing_hash_row.algo,
                    version=existing_hash_row.version,
                    timestamps=existing_hash_row.timestamps,
                    hashes=existing_hash_row.hashes,
                )
                return seq, existing_hash_row
    try:
        seq, frame_assets = await extract.sample_frames(
            path, out_dir, n=n, probe_result=probe_asset
        )
    except extract.ExtractError as exc:
        logger.warning("frame sampling failed for file %s: %s", file.id, exc)
        return None, None
    for a in frame_assets:
        _persist_asset_row(session, file.id, a)
    hash_row = existing_hash_row
    if hash_row is None:
        hash_row = FrameHash(
            file_id=file.id, algo=seq.algo, version=seq.version,
            timestamps=seq.timestamps, hashes=seq.hashes,
        )
        session.add(hash_row)
        session.commit()
    return seq, hash_row


async def _stage_transcript(
    session: Session,
    file: File,
    audio_asset: extract.ExtractedAsset | None,
    transcriber: Transcriber,
) -> dict[str, Any] | None:
    if audio_asset is None or audio_asset.path is None:
        return None
    existing = _latest_asset(session, file.id, "transcript")
    if existing is not None:
        return existing.payload
    result = await transcriber.transcribe(Path(audio_asset.path))
    payload = result.model_dump(mode="json")
    row = Asset(
        file_id=file.id,
        type="transcript",
        path=None,
        payload=payload,
        input_fingerprint=audio_asset.input_fingerprint,
        tool_meta={},
    )
    session.add(row)
    session.commit()
    return payload


def _check_duplicates(session: Session, file: File, frame_seq: extract.FrameHashSeq) -> None:
    """Log-only dupe check against other files' stored frame hashes. See
    module docstring for why this isn't persisted in this task."""
    others = session.execute(
        select(FrameHash).where(FrameHash.file_id != file.id)
    ).scalars().all()
    for other in others:
        other_seq = extract.FrameHashSeq(
            algo=other.algo, version=other.version,
            timestamps=other.timestamps, hashes=other.hashes,
        )
        similarity = extract.hamming_similarity(frame_seq, other_seq)
        if similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
            logger.warning(
                "possible duplicate: file %s looks like file %s (similarity=%.3f)",
                file.id, other.file_id, similarity,
            )


# -- plugin stage ------------------------------------------------------


def _plugin_fingerprint(
    loaded: LoadedPlugin,
    asset_fingerprints: dict[str, Any],
    claimed: ClaimedIdent,
    ctx: SeriesContext,
) -> str:
    series_digest = xxhash.xxh64(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    payload = {
        "plugin_name": loaded.plugin.name,
        "plugin_version": loaded.plugin.version,
        "plugin_config": loaded.config.model_dump(mode="json") if loaded.config is not None else None,
        "asset_fingerprints": asset_fingerprints,
        "claimed": claimed.model_dump(mode="json"),
        "series_digest": series_digest,
    }
    encoded = json.dumps(payload, sort_keys=True)
    return xxhash.xxh64(encoded.encode()).hexdigest()


async def _run_plugin_stage(
    session: Session,
    job: Job,
    deps: PipelineDeps,
    bundle: AssetBundle,
    claimed: ClaimedIdent,
    ctx: SeriesContext,
    asset_fingerprints: dict[str, Any],
) -> list[PluginOutcome]:
    outcomes: list[PluginOutcome] = []
    for loaded in deps.plugins:
        fp = _plugin_fingerprint(loaded, asset_fingerprints, claimed, ctx)
        cached_row = session.execute(
            select(PluginResultRow).where(
                PluginResultRow.job_id == job.id,
                PluginResultRow.plugin_name == loaded.plugin.name,
                PluginResultRow.input_fingerprint == fp,
            )
        ).scalar_one_or_none()

        if cached_row is not None:
            result = PluginResult(
                status=cached_row.status,
                reason=cached_row.reason,
                candidates=[Candidate.model_validate(c) for c in cached_row.candidates],
            )
        else:
            try:
                result = await loaded.plugin.identify(claimed, bundle, ctx)
            except Exception as exc:  # a plugin bug must not kill the job
                logger.exception("plugin %s raised during identify", loaded.plugin.name)
                result = PluginResult(status="error", reason=f"plugin raised: {exc!r}")

            normalized_for_row = [normalize(c, ctx, claimed) for c in result.candidates]
            row = PluginResultRow(
                job_id=job.id,
                plugin_name=loaded.plugin.name,
                plugin_version=loaded.plugin.version,
                status=result.status,
                reason=result.reason,
                candidates=[c.model_dump(mode="json") for c in result.candidates],
                normalized=[n.model_dump(mode="json") for n in normalized_for_row],
                input_fingerprint=fp,
            )
            session.add(row)
            session.commit()

        normalized = [normalize(c, ctx, claimed) for c in result.candidates]
        outcomes.append(
            PluginOutcome(
                plugin_name=loaded.plugin.name, weight=loaded.weight,
                result=result, normalized=normalized,
            )
        )
    return outcomes


# -- phash corpus --------------------------------------------------------


def _external_ids_from_series(series: dict[str, Any]) -> dict[str, Any]:
    ids: dict[str, Any] = {}
    if series.get("tvdb_id") is not None:
        ids["tvdb"] = series["tvdb_id"]
    if series.get("tmdb_id") is not None:
        ids["tmdb"] = series["tmdb_id"]
    if series.get("imdb_id") is not None:
        ids["imdb"] = series["imdb_id"]
    return ids


def _write_phash_corpus(
    session: Session,
    frame_hash_row: FrameHash,
    series: dict[str, Any],
    episode_ids: Any,
    episodes: list[dict[str, Any]],
    confidence: float,
) -> None:
    resolved = _season_and_numbers(episode_ids, episodes)
    if resolved is None:
        logger.warning(
            "phash corpus: could not resolve season/episode numbers for ids %s",
            sorted(episode_ids),
        )
        return
    season, numbers = resolved
    entry = PhashCorpusEntry(
        frame_hash_id=frame_hash_row.id,
        external_ids=_external_ids_from_series(series),
        season=season,
        episodes=numbers,
        confidence=confidence,
        source="auto",
    )
    session.add(entry)
    session.commit()


# -- top-level -----------------------------------------------------------


async def process_job(job_id: int, deps: PipelineDeps) -> None:
    """Run one job through the full pipeline. `deps.worker_id` must already
    hold the job's active lease (the caller's claim) — this function trusts
    that and does not re-validate job status itself.

    The body runs under a catch-all (see module docstring): any exception
    not already handled explicitly below releases the job to `error` rather
    than leaving it `active` for the reaper. `jobs.LeaseLost` and
    `asyncio.CancelledError` are re-raised, not swallowed — both are the
    caller's (worker.py's) concern, not a pipeline failure.
    """
    with deps.session_factory() as session:
        job = session.get(Job, job_id)
        file = session.get(File, job.file_id)

        try:
            ctx = await _get_series_context(deps, file.series_id)
            claimed = _build_claimed_ident(file, ctx.episodes)
            if claimed is None:
                logger.error(
                    "job %s: none of file.episode_ids=%s matched series %s episodes",
                    job_id, file.episode_ids, file.series_id,
                )
                jobs.release(session, job, "error", deps.worker_id)
                _publish(deps, job_id, "error")
                return

            path = Path(file.local_path)
            out_dir = deps.settings.assets_dir / str(file.id)

            probe_asset = await _stage_probe(session, file, path)
            audio_asset = await _stage_audio(session, file, path, out_dir, probe_asset)
            subs_assets = await _stage_subs(session, file, path, out_dir, probe_asset)
            frame_seq, frame_hash_row = await _stage_frames(
                session, file, path, out_dir, probe_asset
            )

            if sum(x is None for x in (probe_asset, audio_asset, subs_assets, frame_seq)) == 4:
                logger.error("job %s: all asset extraction stages failed", job_id)
                jobs.release(session, job, "error", deps.worker_id)
                _publish(deps, job_id, "error")
                return

            transcript_payload = await _stage_transcript(
                session, file, audio_asset, deps.transcriber
            )

            if frame_seq is not None:
                _check_duplicates(session, file, frame_seq)

            bundle = AssetBundle(
                probe=probe_asset.payload if probe_asset else None,
                audio_path=audio_asset.path if audio_asset else None,
                transcript=transcript_payload,
                sub_paths=[a.path for a in subs_assets] if subs_assets else [],
                frame_hashes=frame_seq.model_dump(mode="json") if frame_seq else None,
            )
            asset_fingerprints = {
                "probe": probe_asset.input_fingerprint if probe_asset else None,
                "audio": audio_asset.input_fingerprint if audio_asset else None,
                "subs": [a.input_fingerprint for a in subs_assets] if subs_assets else [],
                "transcript": (
                    audio_asset.input_fingerprint if transcript_payload is not None else None
                ),
                "frames": xxhash.xxh64(":".join(frame_seq.hashes).encode()).hexdigest()
                if frame_seq else None,
            }

            outcomes = await _run_plugin_stage(
                session, job, deps, bundle, claimed, ctx, asset_fingerprints
            )

            sheet = aggregate(outcomes, frozenset(claimed.episode_ids))
            flags = InstanceFlags(
                auto_remap=deps.instance_cfg.auto_remap, auto_replace=deps.instance_cfg.auto_replace
            )
            decision = route(sheet, deps.settings.thresholds, flags)

            proposed_action = None
            if not decision.auto and decision.action is not None:
                proposed_action = decision.action.model_dump(mode="json")

            verdict = Verdict(
                job_id=job.id,
                s_claimed=sheet.s_claimed,
                s_alt=sheet.s_alt,
                outcome=decision.outcome,
                proposed_action=proposed_action,
                source="auto",
            )
            session.add(verdict)
            session.commit()

            final_status = decision.outcome
            if decision.outcome in ("matched", "quarantine", "inconclusive"):
                jobs.release(session, job, decision.outcome, deps.worker_id)
            else:  # "remediate" -- route() only sets this outcome when auto is True.
                remediator = Remediator(
                    deps.sonarr_client, deps.instance_cfg, deps.session_factory
                )
                if isinstance(decision.action, Remap):
                    await remediator.remap(
                        job, decision.action.target_episode_ids, deps.worker_id
                    )
                else:
                    await remediator.replace(job, deps.worker_id)
                with deps.session_factory() as check_session:
                    final_status = check_session.get(Job, job.id).status
            _publish(deps, job_id, final_status)

            if frame_hash_row is not None:
                threshold = deps.settings.thresholds.phash_store
                if (
                    final_status == "matched"
                    and sheet.s_claimed is not None
                    and sheet.s_claimed >= threshold
                ):
                    _write_phash_corpus(
                        session, frame_hash_row, ctx.series, claimed.episode_ids, ctx.episodes,
                        sheet.s_claimed,
                    )
                elif (
                    final_status == "remediated"
                    and isinstance(decision.action, Remap)
                    and sheet.s_alt is not None
                    and sheet.s_alt >= threshold
                ):
                    _write_phash_corpus(
                        session, frame_hash_row, ctx.series, decision.action.target_episode_ids,
                        ctx.episodes, sheet.s_alt,
                    )
        except jobs.LeaseLost:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job %s: unexpected pipeline error", job_id)
            if job.status == "active":
                jobs.release(session, job, "error", deps.worker_id)
                _publish(deps, job_id, "error")
