from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from impostarr.assets import extract
from impostarr.assets.transcribe import (
    NullTranscriber,
    TranscribeError,
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
)
from impostarr.config import Settings, SonarrInstance, Thresholds, ThrottleConfig, TrashConfig
from impostarr.db import init_db, make_session_factory
from impostarr.jobs import LeaseLost, claim_next, reap_stale, release
from impostarr.models import (
    Asset,
    File,
    FrameHash,
    Instance,
    Job,
    PhashCorpusEntry,
    TrashItem,
    Verdict,
)
from impostarr.models import PluginResult as PluginResultRow
from impostarr.pipeline import PipelineDeps, _stage_transcript, process_job
from impostarr.plugins.base import (
    AssetBundle,
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)
from impostarr.plugins.loader import LoadedPlugin
from impostarr.sonarr import SonarrClient
from impostarr.worker import WorkerPool, _mint_worker_id

BASE_URL = "http://sonarr.test:8989"
API_URL = f"{BASE_URL}/api/v3"
API_KEY = "test-api-key"
WORKER_ID = "worker-1"


# -- fixtures / helpers -----------------------------------------------------


@pytest.fixture
def session_factory(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")
    engine = init_db(settings)
    return make_session_factory(engine)


def make_instance_cfg(tmp_path, **overrides) -> SonarrInstance:
    defaults: dict = {
        "name": "main",
        "url": BASE_URL,
        "api_key": API_KEY,
        "staging_dir": str(tmp_path / "staging"),
    }
    defaults.update(overrides)
    return SonarrInstance(**defaults)


def make_deps(
    session_factory,
    tmp_path,
    plugins: list[LoadedPlugin],
    *,
    instance_cfg: SonarrInstance | None = None,
    thresholds: Thresholds | None = None,
    transcriber: Transcriber | None = None,
    approval_required: bool = False,
    throttle: ThrottleConfig | None = None,
) -> PipelineDeps:
    cfg = instance_cfg or make_instance_cfg(tmp_path)
    settings = Settings(
        state_dir=tmp_path / "state",
        assets_dir=tmp_path / "assets",
        thresholds=thresholds or Thresholds(),
        approval_required=approval_required,
        # Settings.trash defaults to /trash (real, unwritable in tests) —
        # every pipeline test that reaches Remediator.replace needs a
        # trash dir it can actually write to, same as state_dir/assets_dir
        # above.
        trash=TrashConfig(dir=tmp_path / "trash"),
        throttle=throttle or ThrottleConfig(),
    )
    client = SonarrClient(BASE_URL, API_KEY, backoff=(0, 0, 0))
    return PipelineDeps(
        session_factory=session_factory,
        sonarr_client=client,
        settings=settings,
        instance_cfg=cfg,
        plugins=plugins,
        transcriber=transcriber if transcriber is not None else NullTranscriber(),
        refsubs=None,
        worker_id=WORKER_ID,
    )


def make_pending_job(
    session_factory,
    tmp_path,
    *,
    episode_ids: list[int] = (555,),
    series_id: int = 42,
    episode_file_id: int = 9001,
    history_id: int | None = None,
) -> tuple[int, Path]:
    content = b"fake media content"
    local_path = tmp_path / "media" / "Show" / "S01E02.mkv"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)

    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL)
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E02.mkv",
            local_path=str(local_path),
            size=len(content),
            content_hash="abc123",
            series_id=series_id,
            episode_ids=list(episode_ids),
            episode_file_id=episode_file_id,
            quality={"quality": {"id": 7}},
            languages=[{"id": 1}],
            history_id=history_id,
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

    with session_factory() as session:
        claim_next(session, WORKER_ID)

    return job_id, local_path


def reclaim_active(session_factory, job_id: int) -> None:
    """Flip a terminal job back to active + WORKER_ID, bypassing jobs.py, so
    `process_job` can be run a second time in the same test (pipeline
    caching tests only, not exercising the queue state machine itself)."""
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "active"
        job.claimed_by = WORKER_ID
        job.claimed_at = datetime.now(UTC)
        job.heartbeat_at = datetime.now(UTC)
        session.commit()


def get_job(session_factory, job_id: int) -> Job:
    with session_factory() as session:
        return session.get(Job, job_id)


def get_verdicts(session_factory, job_id: int) -> list[Verdict]:
    with session_factory() as session:
        return list(session.execute(select(Verdict).where(Verdict.job_id == job_id)).scalars())


def series_json(series_id=42, tvdb_id=1000):
    return {
        "id": series_id,
        "title": "Show",
        "tvdbId": tvdb_id,
        "tmdbId": None,
        "imdbId": None,
        "titleSlug": "show",
    }


def episode_json(id_, *, season_number=1, episode_number=1):
    return {
        "id": id_,
        "seasonNumber": season_number,
        "episodeNumber": episode_number,
        "episodeFileId": 0,
        "hasFile": True,
    }


def mock_series_and_episodes(series_id, episodes: list[dict]):
    respx.get(f"{API_URL}/series/{series_id}").mock(
        return_value=httpx.Response(200, json=series_json(series_id=series_id))
    )
    respx.get(f"{API_URL}/episode", params={"seriesId": str(series_id)}).mock(
        return_value=httpx.Response(200, json=episodes)
    )


# -- canned extractors (no real ffmpeg) --------------------------------


class ExtractorCounters:
    def __init__(self) -> None:
        self.probe = 0
        self.audio = 0
        self.subs = 0
        self.frames = 0


DURATION_S = 1800.0


def install_fake_extractors(monkeypatch) -> ExtractorCounters:
    counters = ExtractorCounters()

    async def fake_probe(path):
        counters.probe += 1
        return extract.ExtractedAsset(
            type="probe",
            payload={"format": {"duration": str(DURATION_S)}, "streams": []},
            input_fingerprint=extract.fingerprint(path, "probe", ""),
            tool_meta={"ffprobe_version": "test"},
        )

    async def fake_extract_audio(path, out_dir, offset_s=60.0, duration_s=900.0, probe_result=None):
        counters.audio += 1
        params = f"offset={offset_s}:duration={duration_s}"
        fp = extract.fingerprint(path, "extract_audio", params)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "audio.wav"
        out_path.write_bytes(b"wav")
        # Mirrors extract.extract_audio's real start-computation: 0.0 if the
        # (fake) file is shorter than the requested offset, else offset_s.
        start = 0.0 if DURATION_S <= offset_s else offset_s
        return extract.ExtractedAsset(
            type="audio",
            path=str(out_path),
            input_fingerprint=fp,
            tool_meta={"start_s": start, "offset_s": start},
        )

    async def fake_extract_embedded_subs(path, out_dir, probe_result=None):
        counters.subs += 1
        return []

    async def fake_sample_frames(path, out_dir, n=16, probe_result=None):
        counters.frames += 1
        timestamps = [(i + 0.5) / n * DURATION_S for i in range(n)]
        assets = []
        for i, ts in enumerate(timestamps):
            params = f"n={n}:index={i}:ts={ts:.6f}"
            fp = extract.fingerprint(path, "sample_frames", params)
            assets.append(
                extract.ExtractedAsset(
                    type="frames", path=f"{out_dir}/frame{i}.jpg", input_fingerprint=fp,
                    tool_meta={},
                )
            )
        seq = extract.FrameHashSeq(
            timestamps=timestamps, hashes=[f"{i:016x}" for i in range(n)]
        )
        return seq, assets

    monkeypatch.setattr(extract, "probe", fake_probe)
    monkeypatch.setattr(extract, "extract_audio", fake_extract_audio)
    monkeypatch.setattr(extract, "extract_embedded_subs", fake_extract_embedded_subs)
    monkeypatch.setattr(extract, "sample_frames", fake_sample_frames)
    return counters


@pytest.fixture(autouse=True)
def fake_extractors(monkeypatch):
    return install_fake_extractors(monkeypatch)


# -- configurable fake plugin --------------------------------------------


class ConfigurablePlugin(IdentifierPlugin):
    version = "1.0.0"

    def __init__(self, name: str, result_fn: Callable[..., PluginResult]) -> None:
        super().__init__(config=None)
        self.name = name
        self._result_fn = result_fn
        self.call_count = 0

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        self.call_count += 1
        return self._result_fn(claimed, assets, ctx)


def claimed_only(confidence: float) -> Callable[..., PluginResult]:
    def _fn(claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult:
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=confidence,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )

    return _fn


def claimed_and_alt(
    claimed_confidence: float, alt_season: int, alt_episodes: list[int], alt_confidence: float
) -> Callable[..., PluginResult]:
    def _fn(claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult:
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=claimed_confidence,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                ),
                Candidate(
                    confidence=alt_confidence,
                    ident=CandidateIdent(series="claimed", season=alt_season, episodes=alt_episodes),
                    numbering="tvdb",
                    evidence={},
                ),
            ],
        )

    return _fn


def abstain() -> Callable[..., PluginResult]:
    def _fn(claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult:
        return PluginResult(status="abstain", reason="no evidence")

    return _fn


def abstain_then_ok(confidence: float) -> Callable[..., PluginResult]:
    """Abstains on the first call, matches on every call after -- simulates
    an environmental fix (e.g. refsubs reachability) landing between runs."""
    state = {"calls": 0}

    def _fn(claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult:
        state["calls"] += 1
        if state["calls"] == 1:
            return PluginResult(status="abstain", reason="no evidence")
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=confidence,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )

    return _fn


def raise_then_ok(confidence: float) -> Callable[..., PluginResult]:
    """Raises on the first call (the pipeline turns that into a status="error"
    row), matches on every call after."""
    state = {"calls": 0}

    def _fn(claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext) -> PluginResult:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient plugin failure")
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=confidence,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )

    return _fn


def loaded(plugin: ConfigurablePlugin, weight: float = 1.0) -> LoadedPlugin:
    return LoadedPlugin(plugin=plugin, weight=weight, config=None)


# -- end-to-end terminal outcomes ----------------------------------------


@respx.mock
async def test_matched_outcome(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "matched"
    verdicts = get_verdicts(session_factory, job_id)
    assert len(verdicts) == 1
    assert verdicts[0].outcome == "matched"
    assert verdicts[0].s_claimed == pytest.approx(0.9)


@respx.mock
async def test_quarantine_outcome(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.5))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "quarantine"
    assert get_verdicts(session_factory, job_id)[0].outcome == "quarantine"


@respx.mock
async def test_inconclusive_outcome_when_all_plugins_abstain(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", abstain())
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "inconclusive"
    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.outcome == "inconclusive"
    assert verdict.s_claimed is None


@respx.mock
async def test_unexpected_exception_in_a_stage_helper_fast_fails_to_error(
    tmp_path, session_factory, monkeypatch
):
    """Regression: a deterministic bug in a stage helper must not leave the
    job silently active until the reaper's lease timeout — process_job's
    catch-all should release it to `error` immediately."""
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    async def boom(*args, **kwargs):
        raise RuntimeError("stage helper exploded")

    monkeypatch.setattr("impostarr.pipeline._stage_probe", boom)

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "error"
    assert plugin.call_count == 0


class _FailingTranscriber:
    """Always raises `TranscribeError` — for testing that a transcriber
    backend failure doesn't fail the whole job (matches `ExtractError`
    discipline in the asset-extraction stages)."""

    async def transcribe(self, wav_path) -> TranscriptResult:
        raise TranscribeError("backend unavailable")


@respx.mock
async def test_transcript_stage_tolerates_transcribe_error(tmp_path, session_factory):
    """A transcriber backend failure logs a warning and leaves the
    transcript absent — it must not fail the job, mirroring how
    `_stage_frames`/`_stage_audio`/etc. tolerate `ExtractError`."""
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [loaded(plugin)], transcriber=_FailingTranscriber()
    )

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "matched"
    with session_factory() as session:
        transcript_assets = session.execute(
            select(Asset).where(Asset.type == "transcript")
        ).scalars().all()
    assert transcript_assets == []


class _FakeTranscriber:
    """Returns a fixed, slice-relative (0-based) `TranscriptResult` --
    stands in for a real backend, which always reports timestamps relative
    to the audio slice it was handed, never the whole file."""

    def __init__(self, segments: list[TranscriptSegment]) -> None:
        self._segments = segments

    async def transcribe(self, wav_path) -> TranscriptResult:
        return TranscriptResult(segments=list(self._segments), language="en")


def _get_file(session_factory, job_id: int) -> File:
    with session_factory() as session:
        job = session.get(Job, job_id)
        return session.get(File, job.file_id)


async def test_stage_transcript_shifts_segments_by_audio_offset(tmp_path, session_factory):
    """Defect 1 regression: whisper's segment timestamps are relative to the
    audio slice `extract_audio` sliced out, not the whole file. `offset_s`
    on the audio asset's tool_meta (here 60.0, matching AUDIO_OFFSET_S) must
    be added into every segment's start/end before the transcript is
    persisted, so stored/UI timestamps are absolute file time."""
    job_id, _ = make_pending_job(session_factory, tmp_path)
    file = _get_file(session_factory, job_id)
    audio_asset = extract.ExtractedAsset(
        type="audio",
        path=str(tmp_path / "audio.wav"),
        input_fingerprint="fp-audio-nonzero",
        tool_meta={"offset_s": 60.0, "start_s": 60.0},
    )
    transcriber = _FakeTranscriber(
        [
            TranscriptSegment(start=0.0, end=2.0, text="hello"),
            TranscriptSegment(start=2.0, end=4.5, text="world"),
        ]
    )

    with session_factory() as session:
        payload = await _stage_transcript(session, file, audio_asset, transcriber)

    assert payload["segments"] == [
        {"start": 60.0, "end": 62.0, "text": "hello"},
        {"start": 62.0, "end": 64.5, "text": "world"},
    ]
    with session_factory() as session:
        row = session.execute(
            select(Asset).where(Asset.type == "transcript")
        ).scalars().one()
    assert row.tool_meta["offset_s"] == 60.0


async def test_stage_transcript_zero_offset_unchanged(tmp_path, session_factory):
    """Zero-offset case (file shorter than the audio-slice offset, or a
    pre-slice source): segments must be stored exactly as the transcriber
    returned them -- no accidental shift."""
    job_id, _ = make_pending_job(session_factory, tmp_path)
    file = _get_file(session_factory, job_id)
    audio_asset = extract.ExtractedAsset(
        type="audio",
        path=str(tmp_path / "audio.wav"),
        input_fingerprint="fp-audio-zero",
        tool_meta={"offset_s": 0.0, "start_s": 0.0},
    )
    transcriber = _FakeTranscriber([TranscriptSegment(start=1.0, end=3.0, text="hi")])

    with session_factory() as session:
        payload = await _stage_transcript(session, file, audio_asset, transcriber)

    assert payload["segments"] == [{"start": 1.0, "end": 3.0, "text": "hi"}]
    with session_factory() as session:
        row = session.execute(
            select(Asset).where(Asset.type == "transcript")
        ).scalars().one()
    assert row.tool_meta["offset_s"] == 0.0


@respx.mock
async def test_auto_replace_invokes_remediator(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path, history_id=None)

    delete_route = respx.delete(f"{API_URL}/episodefile/9001").mock(
        return_value=httpx.Response(200, json={})
    )
    command_route = respx.post(f"{API_URL}/command").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "EpisodeSearch"})
    )

    plugins = [
        loaded(ConfigurablePlugin("fake-a", claimed_only(0.05))),
        loaded(ConfigurablePlugin("fake-b", claimed_only(0.05))),
    ]
    cfg = make_instance_cfg(tmp_path, auto_replace=True, auto_remap=False)
    deps = make_deps(session_factory, tmp_path, plugins, instance_cfg=cfg)

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert delete_route.called
    assert command_route.called
    job = get_job(session_factory, job_id)
    assert job.status == "remediated"
    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.outcome == "remediate"


@respx.mock
async def test_approval_required_demotes_otherwise_auto_replace_to_quarantine(tmp_path, session_factory):
    # Same fixture as test_auto_replace_invokes_remediator (auto_replace=True,
    # two low-confidence plugins clearing auto_min_evidence), minus
    # approval_required. With approval_required=True the pipeline must never
    # invoke the remediator: outcome demotes to quarantine with the proposed
    # replace action queued for human approval instead.
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path, history_id=None)

    delete_route = respx.delete(f"{API_URL}/episodefile/9001")
    command_route = respx.post(f"{API_URL}/command")

    plugins = [
        loaded(ConfigurablePlugin("fake-a", claimed_only(0.05))),
        loaded(ConfigurablePlugin("fake-b", claimed_only(0.05))),
    ]
    cfg = make_instance_cfg(tmp_path, auto_replace=True, auto_remap=False)
    deps = make_deps(session_factory, tmp_path, plugins, instance_cfg=cfg, approval_required=True)

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert not delete_route.called
    assert not command_route.called
    job = get_job(session_factory, job_id)
    assert job.status == "quarantine"
    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.outcome == "quarantine"
    assert verdict.proposed_action is not None
    assert verdict.proposed_action["kind"] == "replace"


@respx.mock
async def test_proposed_remap_when_auto_remap_disabled(tmp_path, session_factory):
    mock_series_and_episodes(
        42,
        [
            episode_json(555, season_number=1, episode_number=2),
            episode_json(556, season_number=1, episode_number=3),
        ],
    )
    job_id, _ = make_pending_job(session_factory, tmp_path)

    plugin = ConfigurablePlugin("fake", claimed_and_alt(0.05, 1, [3], 0.9))
    cfg = make_instance_cfg(tmp_path, auto_remap=False, auto_replace=False)
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)], instance_cfg=cfg)

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    job = get_job(session_factory, job_id)
    assert job.status == "quarantine"
    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.outcome == "quarantine"
    assert verdict.proposed_action is not None
    assert verdict.proposed_action["kind"] == "remap"
    assert set(verdict.proposed_action["target_episode_ids"]) == {556}


# -- caching --------------------------------------------------------------


@respx.mock
async def test_plugin_results_cached_across_runs(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    assert plugin.call_count == 1

    reclaim_active(session_factory, job_id)
    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert plugin.call_count == 1

    with session_factory() as session:
        rows = session.execute(
            select(PluginResultRow).where(PluginResultRow.job_id == job_id)
        ).scalars().all()
        assert len(rows) == 1


@respx.mock
async def test_cached_abstain_is_not_reused_plugin_reexecutes(tmp_path, session_factory):
    # Abstain/error are environmental/transient verdicts, not legitimately
    # cacheable -- re-running an inconclusive job after e.g. a refsubs
    # outage is fixed must actually re-consult the plugin, not replay the
    # stale abstain.
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", abstain_then_ok(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    assert plugin.call_count == 1
    assert get_job(session_factory, job_id).status == "inconclusive"

    reclaim_active(session_factory, job_id)
    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert plugin.call_count == 2
    assert get_job(session_factory, job_id).status == "matched"

    with session_factory() as session:
        rows = session.execute(
            select(PluginResultRow).where(PluginResultRow.job_id == job_id).order_by(PluginResultRow.id)
        ).scalars().all()
        assert [r.status for r in rows] == ["abstain", "ok"]


@respx.mock
async def test_cached_error_is_not_reused_plugin_reexecutes(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", raise_then_ok(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    assert plugin.call_count == 1

    reclaim_active(session_factory, job_id)
    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert plugin.call_count == 2
    assert get_job(session_factory, job_id).status == "matched"

    with session_factory() as session:
        rows = session.execute(
            select(PluginResultRow).where(PluginResultRow.job_id == job_id).order_by(PluginResultRow.id)
        ).scalars().all()
        assert [r.status for r in rows] == ["error", "ok"]


@respx.mock
async def test_plugin_daily_budget_synthesizes_abstain_after_reached(tmp_path, session_factory):
    mock_series_and_episodes(
        42, [episode_json(555, episode_number=2), episode_json(556, episode_number=3)]
    )
    job1_id, _ = make_pending_job(session_factory, tmp_path, episode_ids=(555,), episode_file_id=9001)

    # A second file/job under the same instance -- Instance.name is
    # unique, so inserted by hand rather than a second make_pending_job
    # call. The daily budget counts EXECUTIONS across all jobs, so a
    # second, distinct job is needed to exercise "budget reached partway
    # through the day" rather than a single job's own plugin-result cache.
    content = b"z"
    local_path2 = tmp_path / "media2.mkv"
    local_path2.write_bytes(content)
    with session_factory() as session:
        instance = session.execute(select(Instance).where(Instance.name == "main")).scalar_one()
        file2 = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E03.mkv",
            local_path=str(local_path2),
            size=len(content),
            content_hash="def456",
            series_id=42,
            episode_ids=[556],
            episode_file_id=9002,
            quality={"quality": {"id": 7}},
            languages=[{"id": 1}],
        )
        session.add(file2)
        session.flush()
        job2 = Job(file_id=file2.id, status="pending")
        session.add(job2)
        session.commit()
        job2_id = job2.id
    with session_factory() as session:
        claim_next(session, WORKER_ID)  # claims job2 (job1 already active)

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [LoadedPlugin(plugin=plugin, weight=1.0, config=None, daily_budget=1)]
    )

    await process_job(job1_id, deps)
    assert plugin.call_count == 1
    assert get_job(session_factory, job1_id).status == "matched"

    await process_job(job2_id, deps)
    await deps.sonarr_client.close()

    # Budget (1/day) already spent by job1 -- job2's execution is skipped
    # and synthesized as an abstain instead of calling the plugin again.
    assert plugin.call_count == 1
    assert get_job(session_factory, job2_id).status == "inconclusive"

    with session_factory() as session:
        rows = session.execute(
            select(PluginResultRow).where(PluginResultRow.job_id == job2_id)
        ).scalars().all()
        assert rows == []  # budget-skip is not persisted as a plugin_results row


@respx.mock
async def test_plugin_daily_budget_resets_after_utc_day_rollover(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)

    # A plugin_results row from "yesterday" must not count toward today's
    # budget.
    with session_factory() as session:
        row = PluginResultRow(
            job_id=job_id,
            plugin_name="fake",
            plugin_version="1.0.0",
            status="ok",
            candidates=[],
            normalized=[],
            input_fingerprint="stale",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(row)
        session.commit()

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [LoadedPlugin(plugin=plugin, weight=1.0, config=None, daily_budget=1)]
    )

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert plugin.call_count == 1
    assert get_job(session_factory, job_id).status == "matched"


@respx.mock
async def test_asset_extraction_cached_across_runs(tmp_path, session_factory, fake_extractors):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    counts_after_first = (
        fake_extractors.probe, fake_extractors.audio, fake_extractors.subs, fake_extractors.frames
    )
    assert counts_after_first[0] == 1
    assert counts_after_first[1] == 1
    assert counts_after_first[3] == 1

    reclaim_active(session_factory, job_id)
    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert (
        fake_extractors.probe, fake_extractors.audio, fake_extractors.subs, fake_extractors.frames
    ) == counts_after_first


# -- phash corpus ----------------------------------------------------------


@respx.mock
async def test_phash_corpus_written_on_matched_above_threshold(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)], thresholds=Thresholds(phash_store=0.9))

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert get_job(session_factory, job_id).status == "matched"
    with session_factory() as session:
        entries = session.execute(select(PhashCorpusEntry)).scalars().all()
    assert len(entries) == 1
    assert entries[0].confidence == pytest.approx(0.9)
    assert entries[0].episodes == [2]


@respx.mock
async def test_phash_corpus_not_written_below_threshold(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [loaded(plugin)], thresholds=Thresholds(phash_store=0.95)
    )

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    assert get_job(session_factory, job_id).status == "matched"
    with session_factory() as session:
        entries = session.execute(select(PhashCorpusEntry)).scalars().all()
    assert entries == []


# -- dupe check -------------------------------------------------------------


@respx.mock
async def test_dupe_info_persisted_on_verdict_when_similar_frame_hash_exists(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)

    # The fake frame extractor (install_fake_extractors) always produces the
    # same deterministic hashes ("0000000000000000".."000000000000000f"), so
    # any other file's stored FrameHash with the same hash sequence hits the
    # dupe threshold (hamming_similarity == 1.0) deterministically.
    with session_factory() as session:
        job = session.get(Job, job_id)
        this_file = session.get(File, job.file_id)
        other_file = File(
            instance_id=this_file.instance_id,
            sonarr_path="/tv/Other/S01E09.mkv",
            local_path="/media/tv/Other/S01E09.mkv",
            size=1,
            content_hash="other-hash",
            series_id=42,
            episode_ids=[999],
            episode_file_id=8888,
            quality={},
            languages=[],
        )
        session.add(other_file)
        session.flush()
        session.add(
            FrameHash(
                file_id=other_file.id,
                algo="phash",
                version=1,
                timestamps=[(i + 0.5) / 16 * DURATION_S for i in range(16)],
                hashes=[f"{i:016x}" for i in range(16)],
            )
        )
        session.commit()
        other_file_id = other_file.id

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.dupe_info is not None
    assert verdict.dupe_info["duplicate_of_file_id"] == other_file_id
    assert verdict.dupe_info["similarity"] == pytest.approx(1.0)
    assert verdict.dupe_info["sonarr_path"] == "/tv/Other/S01E09.mkv"


@respx.mock
async def test_dupe_info_absent_when_no_similar_frame_hash(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    await process_job(job_id, deps)
    await deps.sonarr_client.close()

    verdict = get_verdicts(session_factory, job_id)[0]
    assert verdict.dupe_info is None


# -- worker pool ------------------------------------------------------------


@respx.mock
async def test_worker_pool_processes_seeded_job_end_to_end(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    # Undo the claim from make_pending_job: the pool must do its own claim.
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        session.commit()

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])

    pool = WorkerPool({"main": deps}, {}, pool_size=1, lease_timeout_s=5.0)
    await pool.start()
    try:
        for _ in range(100):
            if get_job(session_factory, job_id).status != "active" and plugin.call_count > 0:
                break
            await asyncio.sleep(0.05)
    finally:
        await pool.stop()
        await deps.sonarr_client.close()

    assert get_job(session_factory, job_id).status == "matched"


@respx.mock
async def test_paused_pool_does_not_claim_jobs(tmp_path, session_factory):
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        session.commit()

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)], throttle=ThrottleConfig(paused=True))
    pool = WorkerPool({"main": deps}, {}, pool_size=1, lease_timeout_s=5.0)
    await pool.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await pool.stop()
        await deps.sonarr_client.close()

    assert get_job(session_factory, job_id).status == "pending"
    assert plugin.call_count == 0


@respx.mock
async def test_active_hours_window_blocks_claims_outside_window(tmp_path, session_factory, monkeypatch):
    from impostarr import worker as worker_module

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(worker_module, "datetime", _FixedDatetime)

    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        session.commit()

    # "12" (noon UTC, per the fixed clock above) is outside "00-01".
    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [loaded(plugin)], throttle=ThrottleConfig(active_hours="00-01")
    )
    pool = WorkerPool({"main": deps}, {}, pool_size=1, lease_timeout_s=5.0)
    await pool.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await pool.stop()
        await deps.sonarr_client.close()

    assert get_job(session_factory, job_id).status == "pending"
    assert plugin.call_count == 0


@respx.mock
async def test_jobs_per_hour_throttle_limits_claims(tmp_path, session_factory):
    mock_series_and_episodes(
        42, [episode_json(555, episode_number=2), episode_json(556, episode_number=3)]
    )
    job1_id, _ = make_pending_job(session_factory, tmp_path, episode_ids=(555,), episode_file_id=9001)
    with session_factory() as session:
        job = session.get(Job, job1_id)
        job.status = "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        session.commit()

    # A second file/job under the SAME instance -- Instance.name is
    # unique, so this is inserted by hand rather than via a second
    # make_pending_job call (which would try to insert a second "main"
    # instance and violate that constraint).
    content = b"z"
    local_path2 = tmp_path / "media2.mkv"
    local_path2.write_bytes(content)
    with session_factory() as session:
        instance = session.execute(select(Instance).where(Instance.name == "main")).scalar_one()
        file2 = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E03.mkv",
            local_path=str(local_path2),
            size=len(content),
            content_hash="def456",
            series_id=42,
            episode_ids=[556],
            episode_file_id=9002,
            quality={"quality": {"id": 7}},
            languages=[{"id": 1}],
        )
        session.add(file2)
        session.flush()
        job2 = Job(file_id=file2.id, status="pending")
        session.add(job2)
        session.commit()
        job2_id = job2.id

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(
        session_factory, tmp_path, [loaded(plugin)], throttle=ThrottleConfig(jobs_per_hour=1)
    )
    pool = WorkerPool({"main": deps}, {}, pool_size=1, lease_timeout_s=5.0)
    await pool.start()
    try:
        for _ in range(100):
            if get_job(session_factory, job1_id).status == "matched":
                break
            await asyncio.sleep(0.05)
        # The rate limiter allows only one claim per hour -- give the pool
        # a further beat to (incorrectly) claim job2 if the limiter isn't
        # actually enforced, then assert it didn't.
        await asyncio.sleep(0.3)
    finally:
        await pool.stop()
        await deps.sonarr_client.close()

    assert get_job(session_factory, job1_id).status == "matched"
    assert get_job(session_factory, job2_id).status == "pending"


async def test_worker_pool_sweeps_expired_trash_at_startup(tmp_path, session_factory):
    """Regression: the trash sweep must run immediately when the pool
    starts (not only after its first hourly sleep) — otherwise an
    already-expired item sits around for up to an hour on a freshly
    (re)started pool."""
    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL)
        session.add(instance)
        session.commit()
        item = TrashItem(
            instance="main",
            original_path=str(tmp_path / "orig.mkv"),
            trash_path=str(tmp_path / "trash.mkv"),
            size=1,
            series_id=1,
            episode_ids=[1],
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(item)
        session.commit()
        item_id = item.id

    deps = make_deps(session_factory, tmp_path, [])
    pool = WorkerPool({"main": deps}, {}, pool_size=0, lease_timeout_s=5.0)
    await pool.start()
    try:
        for _ in range(100):
            with session_factory() as session:
                if session.get(TrashItem, item_id).deleted_at is not None:
                    break
            await asyncio.sleep(0.05)
    finally:
        await pool.stop()
        await deps.sonarr_client.close()

    with session_factory() as session:
        swept = session.get(TrashItem, item_id)
    assert swept.deleted_at is not None
    assert swept.outcome == "expired"


@respx.mock
async def test_stop_cancels_in_flight_job_promptly(tmp_path, session_factory, monkeypatch):
    """Regression: stop() must not hang. Cancelling the worker-loop task
    also cancels the process_job task it's awaiting (task cancellation
    propagates to whatever a task is currently suspended on), and that
    CancelledError must NOT be swallowed as if it were a lease-lost
    cancellation, or the worker-loop task never actually finishes and
    stop()'s gather() waits forever."""
    mock_series_and_episodes(42, [episode_json(555, episode_number=2)])
    job_id, _ = make_pending_job(session_factory, tmp_path)
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.status = "pending"
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        session.commit()

    started = asyncio.Event()

    async def slow_process_job(job_id, deps):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr("impostarr.worker.process_job", slow_process_job)

    plugin = ConfigurablePlugin("fake", claimed_only(0.9))
    deps = make_deps(session_factory, tmp_path, [loaded(plugin)])
    pool = WorkerPool({"main": deps}, {}, pool_size=1, lease_timeout_s=5.0)

    await pool.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        tasks_snapshot = list(pool._tasks)

        await asyncio.wait_for(pool.stop(), timeout=2.0)
    finally:
        await deps.sonarr_client.close()

    assert all(t.done() for t in tasks_snapshot)


def test_shared_pool_worker_ids_prevent_lease_clobber_across_tasks(tmp_path, session_factory):
    """Regression: two worker-loop tasks in one pool must use distinct
    worker_ids (as minted by WorkerPool.start() via _mint_worker_id), or the
    Task 5 clobber reopens *inside* a single pool: task A claims, stalls
    past the lease timeout, gets reaped, task B reclaims the same job — with
    a shared worker_id, A's later release() would pass the claimed_by fence
    and clobber B. Driven directly against jobs.py with the pool's own
    id-minting scheme, per the escalation note."""
    content = b"x"
    local_path = tmp_path / "media.mkv"
    local_path.write_bytes(content)
    with session_factory() as session:
        instance = Instance(name="main", url=BASE_URL)
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id, sonarr_path="/tv/x.mkv", local_path=str(local_path),
            size=len(content), content_hash="x", series_id=1, episode_ids=[1],
            episode_file_id=1, quality={}, languages=[],
        )
        session.add(file)
        session.flush()
        job = Job(file_id=file.id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

    worker_id_a = _mint_worker_id(WORKER_ID, 0)
    worker_id_b = _mint_worker_id(WORKER_ID, 1)
    assert worker_id_a != worker_id_b

    with session_factory() as session:
        job_a = claim_next(session, worker_id_a)
        assert job_a is not None
        job_a.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        session.commit()
        assert reap_stale(session, lease_timeout_s=60) == 1

    with session_factory() as session:
        job_b = claim_next(session, worker_id_b)
        assert job_b.id == job_id
        assert job_b.claimed_by == worker_id_b

    with session_factory() as session:
        stale_handle = session.get(Job, job_id)
        with pytest.raises(LeaseLost):
            release(session, stale_handle, "matched", worker_id_a)

    final = get_job(session_factory, job_id)
    assert final.claimed_by == worker_id_b
    assert final.status == "active"


# -- stale lease reap --------------------------------------------------------


def test_stale_lease_is_reaped(tmp_path, session_factory):
    job_id, _ = make_pending_job(tmp_path=tmp_path, session_factory=session_factory)
    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "active"
        job.claimed_by = "dead-worker"
        job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        session.commit()

        reaped = reap_stale(session, lease_timeout_s=60)

    assert reaped == 1
    job = get_job(session_factory, job_id)
    assert job.status == "pending"
    assert job.claimed_by is None
