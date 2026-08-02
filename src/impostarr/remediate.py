"""Remediation choreography: `replace` (delete + re-search) and `remap`
(manual-import into the correct episode slot) per the spec's "Remediation
mechanics" section.

Both methods run under a worker's claim on `job` (already `active`) and
require a verdict row to already exist (pipeline guarantees this). Each step
is appended to the verdict's `remediation_log` and committed before the next
step runs, so a crash mid-sequence leaves an accurate partial log. A step
failure (`SonarrError`) stops the sequence and releases the job to
`quarantine` via `jobs.release` (worker-fenced; `LeaseLost` propagates to the
caller). Full success releases the job to `remediated`.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from impostarr import jobs
from impostarr.config import SonarrInstance
from impostarr.models import File, Job, Verdict
from impostarr.sonarr import SonarrClient, SonarrError

logger = logging.getLogger(__name__)


def _log_step(session: Session, verdict: Verdict, step: str, ok: bool, detail: str) -> None:
    """Append a step to `verdict.remediation_log` and commit it.

    `remediation_log` is a `MutableList`-backed JSON column, so the in-place
    `append` is tracked and persisted by the commit.
    """
    verdict.remediation_log.append(
        {"step": step, "ok": ok, "detail": detail, "ts": datetime.now(UTC).isoformat()}
    )
    session.commit()


def _latest_verdict(session: Session, job_id: int) -> Verdict:
    verdict = (
        session.execute(
            select(Verdict).where(Verdict.job_id == job_id).order_by(Verdict.id.desc())
        )
        .scalars()
        .first()
    )
    if verdict is None:
        raise ValueError(f"job {job_id} has no verdict row")
    return verdict


class Remediator:
    """Executes remediation choreography for one Sonarr instance."""

    def __init__(
        self,
        client: SonarrClient,
        instance_cfg: SonarrInstance,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.client = client
        self.instance_cfg = instance_cfg
        self.session_factory = session_factory

    # -- replace ----------------------------------------------------------

    async def replace(self, job: Job, worker_id: str) -> None:
        """Blocklist (if grab history is known) + delete + re-search.

        Step 1: `mark_history_failed` when `file.history_id` is set;
        otherwise logs an "unblocklistable" note with no API call. Step 2:
        `delete_episode_file`. Step 3: `EpisodeSearch` command. A `SonarrError`
        at any step stops the sequence and quarantines the job with the
        partial log preserved.
        """
        with self.session_factory() as session:
            db_job = session.get(Job, job.id)
            file = session.get(File, db_job.file_id)
            verdict = _latest_verdict(session, db_job.id)

            if file.history_id is not None:
                try:
                    await self.client.mark_history_failed(file.history_id)
                except SonarrError as exc:
                    _log_step(session, verdict, "mark_history_failed", False, str(exc))
                    jobs.release(session, db_job, "quarantine", worker_id)
                    return
                _log_step(
                    session,
                    verdict,
                    "mark_history_failed",
                    True,
                    f"history_id={file.history_id}",
                )
            else:
                _log_step(
                    session,
                    verdict,
                    "mark_history_failed",
                    True,
                    "unblocklistable: no grab history captured",
                )

            try:
                await self.client.delete_episode_file(file.episode_file_id)
            except SonarrError as exc:
                _log_step(session, verdict, "delete_episode_file", False, str(exc))
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            _log_step(
                session,
                verdict,
                "delete_episode_file",
                True,
                f"episode_file_id={file.episode_file_id}",
            )

            try:
                await self.client.command("EpisodeSearch", episodeIds=file.episode_ids)
            except SonarrError as exc:
                _log_step(session, verdict, "episode_search", False, str(exc))
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            _log_step(
                session, verdict, "episode_search", True, f"episode_ids={file.episode_ids}"
            )

            jobs.release(session, db_job, "remediated", worker_id)

    # -- remap --------------------------------------------------------------

    async def remap(
        self, job: Job, target_episode_ids: frozenset[int], worker_id: str
    ) -> None:
        """Hardlink into staging under the corrected name, delete the
        original episodeFile, then manual-import the staged copy into
        `target_episode_ids`.

        Defense-in-depth: refuses (quarantine, no mutating calls) if any
        target episode already has a file — routing should have prevented
        this from being called in that case. Any failure after the hardlink
        leaves the staged file in place and quarantines with the staging
        path recorded in the log.
        """
        with self.session_factory() as session:
            db_job = session.get(Job, job.id)
            file = session.get(File, db_job.file_id)
            verdict = _latest_verdict(session, db_job.id)

            episodes = await self.client.episodes(file.series_id)
            target_episodes = sorted(
                (ep for ep in episodes if ep.id in target_episode_ids),
                key=lambda ep: ep.episode_number,
            )
            occupied = [ep for ep in target_episodes if ep.has_file]
            if occupied:
                _log_step(
                    session,
                    verdict,
                    "occupied_check",
                    False,
                    f"refusing remap: target episode(s) already have a file: "
                    f"{[ep.id for ep in occupied]}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            if not target_episodes:
                _log_step(
                    session,
                    verdict,
                    "occupied_check",
                    False,
                    f"no matching episodes found for target ids {sorted(target_episode_ids)}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return

            local_path = Path(file.local_path)
            season = target_episodes[0].season_number
            ep_numbers = [ep.episode_number for ep in target_episodes]
            ep_part = "-".join(f"E{n:02d}" for n in ep_numbers)
            staged_name = f"S{season:02d}{ep_part}{local_path.suffix}"
            staging_dir = Path(self.instance_cfg.staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_path = staging_dir / staged_name

            try:
                os.link(local_path, staged_path)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    shutil.copy2(local_path, staged_path)
                else:
                    _log_step(session, verdict, "hardlink", False, str(exc))
                    jobs.release(session, db_job, "quarantine", worker_id)
                    return
            _log_step(session, verdict, "hardlink", True, f"staged={staged_path}")

            try:
                await self.client.delete_episode_file(file.episode_file_id)
            except SonarrError as exc:
                _log_step(
                    session,
                    verdict,
                    "delete_episode_file",
                    False,
                    f"{exc}; staging_path={staged_path}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            _log_step(
                session,
                verdict,
                "delete_episode_file",
                True,
                f"episode_file_id={file.episode_file_id}",
            )

            try:
                candidates = await self.client.manual_import_candidates(
                    folder=str(staging_dir)
                )
            except SonarrError as exc:
                _log_step(
                    session,
                    verdict,
                    "manual_import_candidates",
                    False,
                    f"{exc}; staging_path={staged_path}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return

            match = next(
                (c for c in candidates if Path(c.path).name == staged_path.name), None
            )
            if match is None:
                _log_step(
                    session,
                    verdict,
                    "manual_import_candidates",
                    False,
                    f"no candidate matched staged file {staged_path.name}; "
                    f"staging_path={staged_path}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            _log_step(
                session,
                verdict,
                "manual_import_candidates",
                True,
                f"matched candidate path={match.path}",
            )

            files_payload = [
                {
                    "path": match.path,
                    "seriesId": file.series_id,
                    "episodeIds": sorted(target_episode_ids),
                    "quality": match.quality,
                    "languages": match.languages,
                }
            ]
            try:
                await self.client.execute_manual_import(files_payload, import_mode="move")
            except SonarrError as exc:
                _log_step(
                    session,
                    verdict,
                    "execute_manual_import",
                    False,
                    f"{exc}; staging_path={staged_path}",
                )
                jobs.release(session, db_job, "quarantine", worker_id)
                return
            _log_step(
                session,
                verdict,
                "execute_manual_import",
                True,
                f"episode_ids={sorted(target_episode_ids)}",
            )

            jobs.release(session, db_job, "remediated", worker_id)
