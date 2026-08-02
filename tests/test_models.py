from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from impostarr.config import Settings
from impostarr.db import init_db, make_session_factory
from impostarr.models import (
    Asset,
    File,
    FrameHash,
    Instance,
    Job,
    PhashCorpusEntry,
    PluginResult,
    TrashItem,
    Verdict,
)

EXPECTED_TABLES = {
    "instances",
    "jobs",
    "files",
    "assets",
    "plugin_results",
    "verdicts",
    "frame_hashes",
    "phash_corpus",
    "trash_items",
}


@pytest.fixture
def engine(tmp_path):
    settings = Settings(state_dir=tmp_path)
    return init_db(settings)


def test_init_db_creates_schema_on_fresh_sqlite(tmp_path):
    settings = Settings(state_dir=tmp_path)

    engine = init_db(settings)

    assert (tmp_path / "impostarr.db").exists()
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_insert_query_one_row_per_table(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()

        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=123456,
            content_hash="deadbeef",
            series_id=1,
            episode_ids=[10],
            episode_file_id=100,
            quality={"quality": {"name": "WEBDL-1080p"}},
            languages=[{"name": "English"}],
        )
        session.add(file)
        session.flush()

        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.flush()

        asset = Asset(
            file_id=file.id,
            type="probe",
            path="/assets/probe.json",
            input_fingerprint="fp-asset",
            tool_meta={"ffprobe": "6.0"},
        )
        session.add(asset)

        plugin_result = PluginResult(
            job_id=job.id,
            plugin_name="whisper-subs",
            plugin_version="0.1.0",
            status="ok",
            candidates=[{"confidence": 0.9}],
            normalized=[{"episode_ids": [10]}],
            input_fingerprint="fp-plugin",
        )
        session.add(plugin_result)

        verdict = Verdict(
            job_id=job.id,
            s_claimed=0.9,
            s_alt=None,
            outcome="matched",
            source="auto",
        )
        session.add(verdict)

        frame_hash = FrameHash(
            file_id=file.id,
            algo="phash",
            version=1,
            timestamps=[0.5, 1.5],
            hashes=["deadbeef", "beadfeed"],
        )
        session.add(frame_hash)
        session.flush()

        phash_entry = PhashCorpusEntry(
            frame_hash_id=frame_hash.id,
            external_ids={"tvdb": 12345},
            season=1,
            episodes=[1],
            confidence=1.0,
            source="human",
        )
        session.add(phash_entry)

        session.commit()

        assert session.query(Instance).count() == 1
        assert session.query(File).count() == 1
        assert session.query(Job).count() == 1
        assert session.query(Asset).count() == 1
        assert session.query(PluginResult).count() == 1
        assert session.query(Verdict).count() == 1
        assert session.query(FrameHash).count() == 1
        assert session.query(PhashCorpusEntry).count() == 1


def test_files_uniqueness_constraint(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()

        def make_file():
            return File(
                instance_id=instance.id,
                sonarr_path="/tv/Show/S01E01.mkv",
                local_path="/media/tv/Show/S01E01.mkv",
                size=123456,
                content_hash="deadbeef",
                series_id=1,
                episode_ids=[10],
                episode_file_id=100,
                quality={},
                languages=[],
            )

        session.add(make_file())
        session.commit()

        session.add(make_file())
        with pytest.raises(IntegrityError):
            session.commit()


def test_instance_sync_timestamps_round_trip(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.commit()
        instance_id = instance.id
        assert instance.last_polled_at is None
        assert instance.last_backfilled_at is None

    now = datetime.now(UTC)
    with session_factory() as session:
        instance = session.get(Instance, instance_id)
        instance.last_polled_at = now
        instance.last_backfilled_at = now
        session.commit()

    with session_factory() as session:
        reloaded = session.get(Instance, instance_id)
        assert reloaded.last_polled_at == now
        assert reloaded.last_backfilled_at == now


def test_verdict_dupe_info_round_trip(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.flush()

        dupe_info = {"duplicate_of_file_id": 999, "similarity": 0.95, "sonarr_path": "/tv/Other.mkv"}
        verdict = Verdict(job_id=job.id, outcome="quarantine", source="auto", dupe_info=dupe_info)
        session.add(verdict)
        session.commit()
        verdict_id = verdict.id

    with session_factory() as session:
        reloaded = session.get(Verdict, verdict_id)
        assert reloaded.dupe_info == dupe_info


def test_json_columns_round_trip(engine):
    session_factory = make_session_factory(engine)
    cursor = {"series_id": 5, "episode_file_id": 42}

    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989", backfill_cursor=cursor)
        session.add(instance)
        session.commit()
        instance_id = instance.id

    with session_factory() as session:
        reloaded = session.get(Instance, instance_id)
        assert reloaded.backfill_cursor == cursor

    quality = {"quality": {"name": "WEBDL-1080p"}, "revision": {"version": 1}}
    languages = [{"id": 1, "name": "English"}]

    with session_factory() as session:
        instance = session.query(Instance).first()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E02.mkv",
            local_path="/media/tv/Show/S01E02.mkv",
            size=1,
            content_hash="cafebabe",
            series_id=1,
            episode_ids=[11, 12],
            episode_file_id=101,
            quality=quality,
            languages=languages,
        )
        session.add(file)
        session.commit()
        file_id = file.id

    with session_factory() as session:
        reloaded_file = session.get(File, file_id)
        assert reloaded_file.quality == quality
        assert reloaded_file.languages == languages
        assert reloaded_file.episode_ids == [11, 12]


def test_job_timestamps_are_utc_datetimes(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.commit()

        assert isinstance(job.created_at, datetime)
        assert job.created_at.tzinfo is not None
        assert job.created_at <= datetime.now(UTC)


def test_jobs_status_check_constraint_rejects_invalid_value(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="bogus-status")
        session.add(job)
        with pytest.raises(IntegrityError):
            session.commit()


def test_foreign_key_violation_is_enforced_on_sqlite(engine):
    # PRAGMA foreign_keys=ON (set on every connection in db.py) must make
    # SQLite actually enforce FKs — off by default otherwise.
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        job = Job(file_id=999999, status="pending")
        session.add(job)
        with pytest.raises(IntegrityError):
            session.commit()


def test_json_mutable_dict_in_place_mutation_persists(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989", backfill_cursor={"a": 1})
        session.add(instance)
        session.commit()
        instance_id = instance.id

    with session_factory() as session:
        instance = session.get(Instance, instance_id)
        instance.backfill_cursor["b"] = 2  # in-place mutation, no reassignment
        session.commit()

    with session_factory() as session:
        reloaded = session.get(Instance, instance_id)
        assert reloaded.backfill_cursor == {"a": 1, "b": 2}


def test_json_mutable_list_in_place_mutation_persists(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.commit()
        file_id = file.id

    with session_factory() as session:
        file = session.get(File, file_id)
        file.episode_ids.append(2)  # in-place mutation, no reassignment
        session.commit()

    with session_factory() as session:
        reloaded = session.get(File, file_id)
        assert reloaded.episode_ids == [1, 2]


def test_trash_item_round_trip_and_nullable_outcome(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.flush()

        now = datetime.now(UTC)
        item = TrashItem(
            file_id=file.id,
            job_id=job.id,
            instance="main",
            original_path="/media/tv/Show/S01E01.mkv",
            trash_path="/trash/main/S01E01.mkv-1",
            size=123,
            series_id=1,
            episode_ids=[1],
            expires_at=now + timedelta(days=14),
        )
        session.add(item)
        session.commit()  # outcome/deleted_at left None: must not violate the check constraint
        item_id = item.id

    with session_factory() as session:
        reloaded = session.get(TrashItem, item_id)
        assert reloaded.outcome is None
        assert reloaded.deleted_at is None
        assert reloaded.episode_ids == [1]
        assert reloaded.expires_at.tzinfo is not None


def test_trash_item_outcome_check_constraint_rejects_invalid_value(engine):
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        item = TrashItem(
            instance="main",
            original_path="/x",
            trash_path="/trash/x",
            size=1,
            series_id=1,
            episode_ids=[1],
            expires_at=datetime.now(UTC),
            outcome="bogus",
        )
        session.add(item)
        with pytest.raises(IntegrityError):
            session.commit()


def test_file_size_accepts_values_beyond_32_bit_int(engine):
    # File.size is BigInteger — Postgres INTEGER overflows at ~2.1GB, and
    # media files routinely exceed that.
    session_factory = make_session_factory(engine)
    large_size = 8 * 1024**3  # 8 GiB, > 2^31 - 1

    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=large_size,
            content_hash="hash",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.commit()
        file_id = file.id

    with session_factory() as session:
        reloaded = session.get(File, file_id)
        assert reloaded.size == large_size
