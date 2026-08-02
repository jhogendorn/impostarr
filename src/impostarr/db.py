"""Engine/session factory and Alembic-driven schema init for Impostarr."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from impostarr.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_dsn(state_dir: Path) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{state_dir / 'impostarr.db'}"


def resolve_dsn(settings: Settings) -> str:
    """DSN from `Settings.db.dsn`, or a SQLite file under `state_dir` if absent."""
    return settings.db.dsn or _sqlite_dsn(settings.state_dir)


def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(dsn: str) -> Engine:
    # timeout=30: headroom for worker contention on the SQLite file (Task 5's
    # claim/lease queue) rather than immediate "database is locked" errors.
    connect_args = {"timeout": 30} if dsn.startswith("sqlite") else {}
    engine = create_engine(dsn, connect_args=connect_args)
    if dsn.startswith("sqlite"):
        event.listens_for(engine, "connect")(_set_sqlite_pragma)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def init_db(settings: Settings) -> Engine:
    """Build the engine for `settings` and apply Alembic migrations up to head."""
    dsn = resolve_dsn(settings)
    engine = make_engine(dsn)
    command.upgrade(_alembic_config(dsn), "head")
    return engine
