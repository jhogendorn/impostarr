"""SQLAlchemy 2.x declarative models per the PoC spec Data model section.

Portable types only (JSON, not JSONB/ARRAY) — SQLite is the default backend,
Postgres is supported via DSN. All datetimes are UTC and timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


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
    backfill_cursor: Mapped[dict | None] = mapped_column(JSON, default=None)


class File(Base):
    """A discovered episode file and the Sonarr identifiers captured for it."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"))
    sonarr_path: Mapped[str] = mapped_column()
    local_path: Mapped[str] = mapped_column()
    size: Mapped[int] = mapped_column()
    content_hash: Mapped[str] = mapped_column()
    series_id: Mapped[int] = mapped_column()
    episode_ids: Mapped[list] = mapped_column(JSON)
    episode_file_id: Mapped[int] = mapped_column()
    quality: Mapped[dict] = mapped_column(JSON)
    languages: Mapped[list] = mapped_column(JSON)
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
        CheckConstraint(f"status IN {JOB_STATUSES!r}", name="ck_jobs_status"),
    )


class Asset(Base):
    """A cached extraction artifact (probe/audio/subs/frames/transcript) for a file."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    type: Mapped[str] = mapped_column()
    path: Mapped[str | None] = mapped_column(default=None)
    payload: Mapped[dict | None] = mapped_column(JSON, default=None)
    input_fingerprint: Mapped[str] = mapped_column()
    tool_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(f"type IN {ASSET_TYPES!r}", name="ck_assets_type"),
    )


class PluginResult(Base):
    """A single identifier plugin's result for a job, cached by input fingerprint."""

    __tablename__ = "plugin_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    plugin_name: Mapped[str] = mapped_column()
    plugin_version: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column()
    reason: Mapped[str | None] = mapped_column(default=None)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    normalized: Mapped[list] = mapped_column(JSON, default=list)
    input_fingerprint: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(f"status IN {PLUGIN_RESULT_STATUSES!r}", name="ck_plugin_results_status"),
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
    proposed_action: Mapped[dict | None] = mapped_column(JSON, default=None)
    remediation_log: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column()
    human_ident: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(f"source IN {VERDICT_SOURCES!r}", name="ck_verdicts_source"),
    )


class FrameHash(Base):
    """A perceptual frame-hash sequence sampled from a file during asset extraction."""

    __tablename__ = "frame_hashes"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    algo: Mapped[str] = mapped_column()
    version: Mapped[int] = mapped_column()
    timestamps: Mapped[list] = mapped_column(JSON)
    hashes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PhashCorpusEntry(Base):
    """A frame-hash sequence linked to an identified series/episode(s), the phash flywheel corpus."""

    __tablename__ = "phash_corpus"

    id: Mapped[int] = mapped_column(primary_key=True)
    frame_hash_id: Mapped[int] = mapped_column(ForeignKey("frame_hashes.id"))
    external_ids: Mapped[dict] = mapped_column(JSON)
    season: Mapped[int] = mapped_column()
    episodes: Mapped[list] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column()
    source: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint(f"source IN {VERDICT_SOURCES!r}", name="ck_phash_corpus_source"),
    )
