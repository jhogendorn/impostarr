"""Engine construction is lazy (SQLAlchemy doesn't connect at create_engine()
time), so a postgres DSN can be exercised without a live server."""

from __future__ import annotations

from impostarr.db import make_engine


def test_make_engine_postgres_dsn_resolves_dialect_without_connecting():
    engine = make_engine("postgresql+psycopg://user:pass@localhost:5432/impostarr")

    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"
