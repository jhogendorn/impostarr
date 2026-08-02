"""Trash lifecycle: expiring the sweeper drives, plus the delete-now/restore
operations the API exposes over the same rows.

Deliberately independent of `Settings.dry_run`: by the time a `TrashItem`
row exists, the file has already been copied into Impostarr's own trash
mount by `remediate.Remediator.replace` (which *is* dry-run-gated — no rows
are created in dry-run). Sweeping/deleting/restoring those rows afterwards
is Impostarr managing its own trash directory, not a mutation of the media
library Sonarr manages, so none of the functions here check `dry_run` (see
`Settings.dry_run`'s docstring: only Sonarr API mutations and
*media-library* filesystem operations are in scope for that flag).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from impostarr.models import TrashItem

logger = logging.getLogger(__name__)


class RestoreConflict(Exception):
    """Raised by `restore` when a file already exists at `original_path`."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _unlink_tolerant(path: str) -> None:
    """Best-effort unlink: a trash file already gone from disk (manually
    cleaned up, moved, etc.) is logged, not raised — the row still needs to
    be marked so it drops out of the active listing."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        logger.warning("trash file already missing on disk: %s", path)
    except OSError:
        logger.warning("failed to unlink trash file: %s", path, exc_info=True)


def sweep_expired(session: Session) -> int:
    """Permanently delete every `TrashItem` whose `expires_at` has passed
    and hasn't already left the active set (`deleted_at is None`). Returns
    the count swept."""
    now = _utcnow()
    expired = (
        session.execute(
            select(TrashItem).where(TrashItem.expires_at < now, TrashItem.deleted_at.is_(None))
        )
        .scalars()
        .all()
    )
    for item in expired:
        _unlink_tolerant(item.trash_path)
        item.deleted_at = now
        item.outcome = "expired"
    session.commit()
    return len(expired)


def delete_now(session: Session, item: TrashItem) -> None:
    """API-driven early delete: unlink + mark, ignoring the retention
    countdown."""
    _unlink_tolerant(item.trash_path)
    item.deleted_at = _utcnow()
    item.outcome = "deleted"
    session.commit()


def restore(session: Session, item: TrashItem) -> None:
    """Copy/hardlink the trashed file back to `original_path` and mark the
    row restored. Raises `RestoreConflict` (caller maps to 409) if a file
    already exists at `original_path` — never overwrites. Does not re-import
    into Sonarr; that's left to the operator."""
    original = Path(item.original_path)
    if original.exists():
        raise RestoreConflict(f"a file already exists at {original}")
    original.parent.mkdir(parents=True, exist_ok=True)
    trash_path = Path(item.trash_path)
    try:
        os.link(trash_path, original)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            shutil.copy2(trash_path, original)
        else:
            raise
    item.deleted_at = _utcnow()
    item.outcome = "restored"
    session.commit()
