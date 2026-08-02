"""SQLAlchemy 2.x declarative models per the PoC spec Data model section.

Portable types only (JSON, not JSONB/ARRAY) — SQLite is the default backend,
Postgres is supported via DSN. All datetimes are UTC and timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _enum_check(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({allowed})"


# Standard Alembic-recommended convention so unnamed constraints (FKs, PKs)
# get stable, predictable names instead of DB-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


JOB_STATUSES = (
    "hold",
    "pending",
    "active",
    "matched",
    "quarantine",
    "inconclusive",
    "error",
    "remediated",
)
PLUGIN_RESULT_STATUSES = ("ok", "abstain", "error")
VERDICT_SOURCES = ("auto", "human")
ASSET_TYPES = ("probe", "audio", "subs", "frames", "transcript")


class Instance(Base):
    """A configured Sonarr instance and its discovery cursors."""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    url: Mapped[str] = mapped_column()
    history_watermark: Mapped[int | None] = mapped_column(default=None)
    backfill_cursor: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), default=None)


class File(Base):
    """A discovered episode file and the Sonarr identifiers captured for it."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"))
    sonarr_path: Mapped[str] = mapped_column()
    local_path: Mapped[str] = mapped_column()
    size: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column()
    series_id: Mapped[int] = mapped_column()
    episode_ids: Mapped[list] = mapped_column(MutableList.as_mutable(JSON))
    episode_file_id: Mapped[int] = mapped_column()
    quality: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON))
    languages: Mapped[list] = mapped_column(MutableList.as_mutable(JSON))
    history_id: Mapped[int | None] = mapped_column(default=None)
    download_id: Mapped[str | None] = mapped_column(default=None)
    source_title: Mapped[str | None] = mapped_column(default=None)
    indexer: Mapped[str | None] = mapped_column(default=None)
    guid: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("instance_id", "episode_file_id", name="uq_files_instance_episode_file"),
    )


class Job(Base):
    """A unit of work over a captured file, tracked through the queue state machine."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    claimed_by: Mapped[str | None] = mapped_column(default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(_enum_check("status", JOB_STATUSES), name="status_valid"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )


class Asset(Base):
    """A cached extraction artifact (probe/audio/subs/frames/transcript) for a file."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    type: Mapped[str] = mapped_column()
    path: Mapped[str | None] = mapped_column(default=None)
    payload: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), default=None)
    input_fingerprint: Mapped[str] = mapped_column()
    tool_meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (CheckConstraint(_enum_check("type", ASSET_TYPES), name="type_valid"),)


class PluginResult(Base):
    """A single identifier plugin's result for a job, cached by input fingerprint."""

    __tablename__ = "plugin_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    plugin_name: Mapped[str] = mapped_column()
    plugin_version: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column()
    reason: Mapped[str | None] = mapped_column(default=None)
    candidates: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    normalized: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    input_fingerprint: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(_enum_check("status", PLUGIN_RESULT_STATUSES), name="status_valid"),
        Index("ix_plugin_results_input_fingerprint", "input_fingerprint"),
    )


class Verdict(Base):
    """The aggregated score, routing outcome, and remediation history for a job."""

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    s_claimed: Mapped[float | None] = mapped_column(default=None)
    s_alt: Mapped[float | None] = mapped_column(default=None)
    outcome: Mapped[str] = mapped_column()
    proposed_action: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), default=None)
    remediation_log: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    source: Mapped[str] = mapped_column()
    human_ident: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSON), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (CheckConstraint(_enum_check("source", VERDICT_SOURCES), name="source_valid"),)


class FrameHash(Base):
    """A perceptual frame-hash sequence sampled from a file during asset extraction."""

    __tablename__ = "frame_hashes"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    algo: Mapped[str] = mapped_column()
    version: Mapped[int] = mapped_column()
    timestamps: Mapped[list] = mapped_column(MutableList.as_mutable(JSON))
    hashes: Mapped[list] = mapped_column(MutableList.as_mutable(JSON))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PhashCorpusEntry(Base):
    """A frame-hash sequence linked to an identified series/episode(s), the phash flywheel corpus."""

    __tablename__ = "phash_corpus"

    id: Mapped[int] = mapped_column(primary_key=True)
    frame_hash_id: Mapped[int] = mapped_column(ForeignKey("frame_hashes.id"))
    external_ids: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON))
    season: Mapped[int] = mapped_column()
    episodes: Mapped[list] = mapped_column(MutableList.as_mutable(JSON))
    confidence: Mapped[float] = mapped_column()
    source: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (CheckConstraint(_enum_check("source", VERDICT_SOURCES), name="source_valid"),)
