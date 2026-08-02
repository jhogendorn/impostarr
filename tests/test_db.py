"""Engine construction is lazy (SQLAlchemy doesn't connect at create_engine()
time), so a postgres DSN can be exercised without a live server."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from impostarr.config import Settings
from impostarr.db import _alembic_config, init_db, make_engine, resolve_dsn


def test_make_engine_postgres_dsn_resolves_dialect_without_connecting():
    engine = make_engine("postgresql+psycopg://user:pass@localhost:5432/impostarr")

    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"


def test_fresh_db_migrates_to_head_with_sync_and_dupe_columns(tmp_path):
    settings = Settings(state_dir=tmp_path)
    engine = init_db(settings)

    insp = sa.inspect(engine)
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    verdict_cols = {c["name"] for c in insp.get_columns("verdicts")}
    assert {"last_polled_at", "last_backfilled_at"} <= instance_cols
    assert "dupe_info" in verdict_cols


def test_downgrade_then_upgrade_round_trip_preserves_head_columns(tmp_path):
    settings = Settings(state_dir=tmp_path)
    engine = init_db(settings)
    dsn = resolve_dsn(settings)
    cfg = _alembic_config(dsn)

    command.downgrade(cfg, "-2")
    insp = sa.inspect(engine)
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    assert "last_polled_at" not in instance_cols
    assert "trash_items" not in insp.get_table_names()

    command.upgrade(cfg, "head")
    insp = sa.inspect(engine)
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    verdict_cols = {c["name"] for c in insp.get_columns("verdicts")}
    assert {"last_polled_at", "last_backfilled_at"} <= instance_cols
    assert "dupe_info" in verdict_cols
    assert "trash_items" in insp.get_table_names()


def test_fresh_db_migrates_to_head_with_trash_items_table(tmp_path):
    settings = Settings(state_dir=tmp_path)
    engine = init_db(settings)

    insp = sa.inspect(engine)
    assert "trash_items" in insp.get_table_names()
    trash_cols = {c["name"] for c in insp.get_columns("trash_items")}
    assert {
        "id", "file_id", "job_id", "instance", "original_path", "trash_path",
        "size", "series_id", "episode_ids", "trashed_at", "expires_at",
        "deleted_at", "outcome",
    } <= trash_cols


def test_trash_items_migration_downgrades_alone(tmp_path):
    """Downgrading just the trash_items revision (-1) must drop only that
    table, leaving the sync-timestamps/dupe-info columns from the prior
    revision intact."""
    settings = Settings(state_dir=tmp_path)
    engine = init_db(settings)
    dsn = resolve_dsn(settings)
    cfg = _alembic_config(dsn)

    command.downgrade(cfg, "-1")
    insp = sa.inspect(engine)
    assert "trash_items" not in insp.get_table_names()
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    assert "last_polled_at" in instance_cols

    command.upgrade(cfg, "head")
    assert "trash_items" in sa.inspect(engine).get_table_names()
