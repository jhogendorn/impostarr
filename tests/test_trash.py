from __future__ import annotations

import errno
import os
from datetime import UTC, datetime, timedelta

import pytest

from impostarr.config import Settings
from impostarr.db import init_db, make_session_factory
from impostarr.models import Instance, TrashItem
from impostarr.trash import RestoreConflict, delete_now, restore, sweep_expired


@pytest.fixture
def session_factory(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    engine = init_db(settings)
    return make_session_factory(engine)


def make_trash_item(session_factory, tmp_path, *, expires_at, content: bytes = b"trashed") -> int:
    trash_path = tmp_path / "trash" / "main" / "S01E01.mkv-1"
    trash_path.parent.mkdir(parents=True, exist_ok=True)
    trash_path.write_bytes(content)

    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr.test:8989")
        session.add(instance)
        session.flush()
        item = TrashItem(
            instance="main",
            original_path=str(tmp_path / "media" / "Show" / "S01E01.mkv"),
            trash_path=str(trash_path),
            size=len(content),
            series_id=1,
            episode_ids=[1],
            expires_at=expires_at,
        )
        session.add(item)
        session.commit()
        return item.id


def get_item(session_factory, item_id):
    with session_factory() as session:
        return session.get(TrashItem, item_id)


# -- sweep_expired ------------------------------------------------------


def test_sweep_expired_unlinks_and_marks_expired(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    trash_path = get_item(session_factory, item_id).trash_path

    count = None
    with session_factory() as session:
        count = sweep_expired(session)

    assert count == 1
    assert not os.path.exists(trash_path)
    item = get_item(session_factory, item_id)
    assert item.outcome == "expired"
    assert item.deleted_at is not None


def test_sweep_expired_leaves_unexpired_items_untouched(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    trash_path = get_item(session_factory, item_id).trash_path

    with session_factory() as session:
        count = sweep_expired(session)

    assert count == 0
    assert os.path.exists(trash_path)
    item = get_item(session_factory, item_id)
    assert item.outcome is None
    assert item.deleted_at is None


def test_sweep_expired_tolerates_missing_file_on_disk(tmp_path, session_factory, caplog):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    os.unlink(get_item(session_factory, item_id).trash_path)

    with session_factory() as session:
        count = sweep_expired(session)

    assert count == 1
    item = get_item(session_factory, item_id)
    assert item.outcome == "expired"
    assert item.deleted_at is not None


def test_sweep_expired_skips_already_deleted_items(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        item.deleted_at = datetime.now(UTC)
        item.outcome = "deleted"
        session.commit()

    with session_factory() as session:
        count = sweep_expired(session)

    assert count == 0


# -- delete_now -----------------------------------------------------------


def test_delete_now_unlinks_and_marks_deleted(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) + timedelta(days=14)
    )
    trash_path = get_item(session_factory, item_id).trash_path

    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        delete_now(session, item)

    assert not os.path.exists(trash_path)
    item = get_item(session_factory, item_id)
    assert item.outcome == "deleted"
    assert item.deleted_at is not None


# -- restore ----------------------------------------------------------------


def test_restore_hardlinks_back_to_original_path(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) + timedelta(days=14), content=b"data"
    )

    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        restore(session, item)
        original_path = item.original_path
        trash_path = item.trash_path

    assert os.path.exists(original_path)
    assert os.stat(original_path).st_ino == os.stat(trash_path).st_ino
    item = get_item(session_factory, item_id)
    assert item.outcome == "restored"
    assert item.deleted_at is not None


def test_restore_conflict_when_file_already_exists_at_original_path(tmp_path, session_factory):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) + timedelta(days=14)
    )
    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        original = tmp_path / "media" / "Show" / "S01E01.mkv"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"already here")

        with pytest.raises(RestoreConflict):
            restore(session, item)

    item = get_item(session_factory, item_id)
    assert item.outcome is None  # unchanged: restore never partially applied


def test_restore_cross_device_fallback_uses_copy(tmp_path, session_factory, monkeypatch):
    item_id = make_trash_item(
        session_factory, tmp_path, expires_at=datetime.now(UTC) + timedelta(days=14), content=b"data"
    )

    def fake_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", fake_link)

    with session_factory() as session:
        item = session.get(TrashItem, item_id)
        restore(session, item)
        original_path = item.original_path

    assert os.path.exists(original_path)
    with open(original_path, "rb") as fh:
        assert fh.read() == b"data"
