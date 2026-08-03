"""Discovery: Sonarr history polling and library backfill.

`Discoverer` turns Sonarr import events (and, for backfill, the existing
library) into `files` rows + `jobs`. Both entry points (`poll_once`,
`backfill_step`) run their whole batch — every file captured plus the
watermark/cursor advance — through a single SQLAlchemy session committed
exactly once at the end. This is why Job rows are constructed directly
here rather than via `jobs.create_job`: that helper commits immediately per
call (see its module docstring), which would let some jobs in a batch land
while a later failure in the same batch rolled the rest back — a
watermark/cursor that no longer matches what's actually in the DB. A single
commit makes the batch all-or-nothing (crash-safe).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import xxhash
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from impostarr.config import PathMapping, SonarrInstance
from impostarr.models import File, Instance, Job
from impostarr.sonarr import SonarrClient, SonarrError
from impostarr.sonarr.types import EpisodeFile

logger = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


class DiscoveryResult(NamedTuple):
    """`poll_once`/`backfill_step`'s per-call outcome. `skipped` counts
    only files skipped for the per-file-tolerance reason (an OSError/
    PermissionError reading the file) -- NOT the other, pre-existing skip
    reasons (dedupe, unmapped path, watch_dirs filter, file missing at
    poll time), which were never errors to begin with."""

    created: int
    skipped: int


def _utcnow() -> datetime:
    return datetime.now(UTC)


def hash_file(path: Path) -> str:
    """xxh64 of a file's first 8MiB + last 8MiB + its size.

    Deliberately cheap and content-position-limited rather than a full-file
    hash: fast on large media files, and stable across remuxes that only
    touch container/interleaving in the middle of the file.
    """
    size = path.stat().st_size
    hasher = xxhash.xxh64()
    with path.open("rb") as fh:
        head = fh.read(HASH_CHUNK_SIZE)
        hasher.update(head)
        if size > HASH_CHUNK_SIZE:
            fh.seek(max(size - HASH_CHUNK_SIZE, 0))
            hasher.update(fh.read(HASH_CHUNK_SIZE))
    hasher.update(str(size).encode())
    return hasher.hexdigest()


class Discoverer:
    """Discovers new/existing Sonarr imports for one instance and files them
    as `jobs` for the queue.

    Note on backfill pacing: `backfill_step` always lands new jobs directly
    in `pending` (never `hold`). A future instance-level rate limiter for
    backfill would use `jobs.park`/`jobs.unpark` to move jobs to `hold`; not
    implemented here (YAGNI for the PoC) — the caller is responsible for
    pacing calls to `backfill_step` itself (e.g. sleeping between steps).
    """

    def __init__(
        self,
        instance_cfg: SonarrInstance,
        client: SonarrClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.instance_cfg = instance_cfg
        self.client = client
        self.session_factory = session_factory

    # -- shared helpers ---------------------------------------------------

    def _get_or_create_instance(self, session: Session) -> Instance:
        instance = session.execute(
            select(Instance).where(Instance.name == self.instance_cfg.name)
        ).scalar_one_or_none()
        if instance is None:
            instance = Instance(
                name=self.instance_cfg.name, url=self.instance_cfg.url, history_watermark=0
            )
            session.add(instance)
            session.commit()
        return instance

    def _map_local_path(self, sonarr_path: str) -> Path | None:
        """Longest sonarr-prefix match over `path_mappings`; None if unmapped."""
        sonarr_posix = PurePosixPath(sonarr_path)
        best: PathMapping | None = None
        for mapping in self.instance_cfg.path_mappings:
            prefix = PurePosixPath(mapping.sonarr)
            is_match = sonarr_posix == prefix or sonarr_posix.is_relative_to(prefix)
            if is_match and (best is None or len(prefix.parts) > len(PurePosixPath(best.sonarr).parts)):
                best = mapping
        if best is None:
            return None
        rel = sonarr_posix.relative_to(PurePosixPath(best.sonarr))
        return Path(best.local) / rel

    def _watch_dirs_allowed(self, local_path: Path) -> bool:
        if not self.instance_cfg.watch_dirs:
            return True
        return any(local_path.is_relative_to(Path(watch_dir)) for watch_dir in self.instance_cfg.watch_dirs)

    def _file_exists(self, session: Session, instance_id: int, episode_file_id: int) -> bool:
        return (
            session.execute(
                select(File.id).where(
                    File.instance_id == instance_id, File.episode_file_id == episode_file_id
                )
            ).first()
            is not None
        )

    async def _capture_file(
        self,
        session: Session,
        instance: Instance,
        *,
        ep_file: EpisodeFile,
        series_id: int,
        episode_ids: list[int] | None,
        quality: dict,
        languages: list,
        history_id: int | None = None,
        download_id: str | None = None,
        source_title: str | None = None,
        indexer: str | None = None,
        guid: str | None = None,
        episode_ids_resolver: Callable[[], Awaitable[list[int]]] | None = None,
    ) -> tuple[bool, bool]:
        """Stage a `files` row + `jobs` row on `session` (no commit).

        Returns `(created, errored)`: `created` is whether a job was
        created (False for any skip reason); `errored` is True only for
        the per-file-tolerance skip (an OSError/PermissionError reading
        the file, e.g. restrictive on-disk permissions) -- an EXPECTED
        condition for a read-only-blast-radius deployment, not a reason to
        fail the whole batch. `episode_ids` is used as-is when given; when
        `None`, `episode_ids_resolver` is awaited instead (kept lazy so a
        file that gets skipped below never triggers the resolver's network
        call)."""
        if self._file_exists(session, instance.id, ep_file.id):
            return False, False

        local_path = self._map_local_path(ep_file.path)
        if local_path is None:
            logger.warning(
                "no path mapping for sonarr path %r (instance=%s), skipping",
                ep_file.path,
                self.instance_cfg.name,
            )
            return False, False

        if not self._watch_dirs_allowed(local_path):
            return False, False

        try:
            exists = local_path.exists()
        except OSError as exc:
            logger.warning("cannot access local file, skipping: %s (%s)", local_path, exc)
            return False, True
        if not exists:
            logger.warning("local file missing, skipping: %s", local_path)
            return False, False

        if episode_ids is None:
            assert episode_ids_resolver is not None
            episode_ids = await episode_ids_resolver()

        try:
            content_hash = hash_file(local_path)
        except OSError as exc:
            logger.warning("cannot read local file, skipping: %s (%s)", local_path, exc)
            return False, True
        file_row = File(
            instance_id=instance.id,
            sonarr_path=ep_file.path,
            local_path=str(local_path),
            size=ep_file.size,
            content_hash=content_hash,
            series_id=series_id,
            episode_ids=episode_ids,
            episode_file_id=ep_file.id,
            quality=quality,
            languages=languages,
            history_id=history_id,
            download_id=download_id,
            source_title=source_title,
            indexer=indexer,
            guid=guid,
        )
        session.add(file_row)
        session.flush()  # assign file_row.id for the Job FK
        session.add(Job(file_id=file_row.id, status="pending"))
        return True, False

    # -- poll ---------------------------------------------------------

    async def poll_once(self) -> DiscoveryResult:
        """Fetch history since the stored watermark and file each new import.
        Returns the count of jobs created (and, separately, files skipped
        for the per-file-tolerance reason — see `DiscoveryResult`).
        Advances the watermark to the highest history id seen this call
        (including skipped records — a skip reason like an unmapped path
        won't be retried forever)."""
        with self.session_factory() as session:
            instance = self._get_or_create_instance(session)
            instance_id = instance.id
            watermark = instance.history_watermark or 0

        records = await self.client.history_since(watermark)
        if not records:
            with self.session_factory() as session:
                instance = session.get(Instance, instance_id)
                instance.last_polled_at = _utcnow()
                session.commit()
            return DiscoveryResult(created=0, skipped=0)

        created = 0
        skipped = 0
        # Per-series episodeFileId -> episode_ids grouping, cached across
        # this call's records (client.episodes(series_id) is fetched at
        # most once per distinct series_id seen in this batch).
        episode_ids_by_series: dict[int, dict[int, list[int]]] = {}
        with self.session_factory() as session:
            instance = session.get(Instance, instance_id)
            last_id = watermark
            for record in records:
                last_id = record.id
                episode_file_id = record.episode_file_id
                if episode_file_id is None:
                    logger.warning(
                        "history record %s has no episode file id, skipping", record.id
                    )
                    continue
                if self._file_exists(session, instance.id, episode_file_id):
                    continue
                try:
                    ep_file = await self.client.episode_file(episode_file_id)
                except SonarrError as exc:
                    if exc.status_code == 404:
                        logger.warning(
                            "episode file %s no longer exists (history id %s), skipping",
                            episode_file_id,
                            record.id,
                        )
                        continue
                    raise
                # Sonarr emits one history record per episode, even for a
                # multi-episode file sharing one episodeFileId: record.
                # episode_ids is always single-element. Resolve the full set
                # lazily (only for files that actually get captured below)
                # by grouping episodeFileId via the episodes endpoint,
                # falling back to the record's own id if the file isn't
                # found there (e.g. already deleted from Sonarr).
                async def resolve_episode_ids(
                    series_id: int = record.series_id,
                    file_id: int = episode_file_id,
                    fallback: list[int] = record.episode_ids,
                ) -> list[int]:
                    if series_id not in episode_ids_by_series:
                        episode_ids_by_series[series_id] = await self._episode_ids_by_file(
                            series_id
                        )
                    return episode_ids_by_series[series_id].get(file_id) or fallback

                captured, errored = await self._capture_file(
                    session,
                    instance,
                    ep_file=ep_file,
                    series_id=record.series_id,
                    episode_ids=None,
                    episode_ids_resolver=resolve_episode_ids,
                    quality=record.quality,
                    languages=record.languages,
                    history_id=record.id,
                    download_id=record.download_id,
                    source_title=record.source_title,
                    indexer=record.indexer,
                    guid=record.guid,
                )
                if captured:
                    created += 1
                if errored:
                    skipped += 1
            instance.history_watermark = last_id
            instance.last_polled_at = _utcnow()
            session.commit()
        return DiscoveryResult(created=created, skipped=skipped)

    # -- backfill -------------------------------------------------------

    async def _episode_ids_by_file(self, series_id: int) -> dict[int, list[int]]:
        episodes = await self.client.episodes(series_id)
        mapping: dict[int, list[int]] = {}
        for episode in episodes:
            if episode.has_file:
                mapping.setdefault(episode.episode_file_id, []).append(episode.id)
        return mapping

    async def backfill_step(
        self, batch_size: int, *, reset: bool = False, series_id: int | None = None
    ) -> DiscoveryResult:
        """Process up to `batch_size` episode files from the existing
        library, resuming from the persisted cursor. Series are walked in
        ascending id order, files within a series in ascending id order.
        Returns the count of jobs created this step (may be less than
        `batch_size` files processed, since dedupe can skip files) and the
        count skipped for the per-file-tolerance reason (see
        `DiscoveryResult`). When every series is exhausted, resets the
        cursor to null and returns 0 created.

        `reset`/`series_id` retarget where the walk starts, as a one-shot
        override of the persisted cursor for this call only (simplest
        correct form of "pre-position the cursor" -- the normal end-of-call
        cursor write then persists wherever this step actually left off,
        so a later call with neither flag resumes from there): `series_id`
        (if given) starts at that series, skipping earlier ones, at its
        first file -- takes precedence over `reset`. Otherwise `reset`
        starts from the very beginning. With neither, resumes from the
        persisted cursor as before.
        """
        with self.session_factory() as session:
            instance = self._get_or_create_instance(session)
            instance_id = instance.id
            cursor = None if reset else instance.backfill_cursor

        all_series = sorted(await self.client.all_series(), key=lambda s: s.id)

        if series_id is not None:
            start_series_id: int | None = series_id
            start_after_file_id: int | None = None
        else:
            start_series_id = cursor["series_id"] if cursor else None
            start_after_file_id = cursor["episode_file_id"] if cursor else None
        if start_series_id is None:
            start_idx = 0
        else:
            start_idx = next(
                (i for i, s in enumerate(all_series) if s.id >= start_series_id),
                len(all_series),
            )

        created = 0
        skipped = 0
        processed = 0
        new_cursor: dict[str, int] | None = None

        with self.session_factory() as session:
            instance = session.get(Instance, instance_id)
            idx = start_idx
            while idx < len(all_series) and processed < batch_size:
                series = all_series[idx]
                episode_ids_by_file = await self._episode_ids_by_file(series.id)
                files = sorted(await self.client.episode_files(series.id), key=lambda f: f.id)
                if series.id == start_series_id and start_after_file_id is not None:
                    files = [f for f in files if f.id > start_after_file_id]

                series_exhausted = True
                for ep_file in files:
                    if processed >= batch_size:
                        series_exhausted = False
                        break
                    processed += 1
                    captured, errored = await self._capture_file(
                        session,
                        instance,
                        ep_file=ep_file,
                        series_id=series.id,
                        episode_ids=episode_ids_by_file.get(ep_file.id, []),
                        quality=ep_file.quality,
                        languages=ep_file.languages,
                    )
                    if captured:
                        created += 1
                    if errored:
                        skipped += 1
                    new_cursor = {"series_id": series.id, "episode_file_id": ep_file.id}

                if not series_exhausted:
                    # Batch cap hit mid-series: stay on this series next call.
                    break
                idx += 1

            if idx >= len(all_series):
                # Walked every series to completion -> backfill complete.
                new_cursor = None

            instance.backfill_cursor = new_cursor
            instance.last_backfilled_at = _utcnow()
            session.commit()
        return DiscoveryResult(created=created, skipped=skipped)
