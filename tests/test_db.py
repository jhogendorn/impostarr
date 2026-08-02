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

    command.downgrade(cfg, "-1")
    insp = sa.inspect(engine)
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    assert "last_polled_at" not in instance_cols

    command.upgrade(cfg, "head")
    insp = sa.inspect(engine)
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    verdict_cols = {c["name"] for c in insp.get_columns("verdicts")}
    assert {"last_polled_at", "last_backfilled_at"} <= instance_cols
    assert "dupe_info" in verdict_cols
