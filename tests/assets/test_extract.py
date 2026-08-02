from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from impostarr.assets import extract

if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    pytest.skip("ffmpeg/ffprobe not found on PATH", allow_module_level=True)


async def test_probe_returns_duration(test_video: Path) -> None:
    asset = await extract.probe(test_video)
    assert asset.type == "probe"
    assert asset.payload is not None
    duration = float(asset.payload["format"]["duration"])
    assert duration == pytest.approx(30, abs=1)
    assert "ffprobe_version" in asset.tool_meta


async def test_extract_audio_is_16khz_mono_starting_at_zero(test_video: Path, tmp_path: Path) -> None:
    asset = await extract.extract_audio(test_video, tmp_path)
    assert asset.path is not None
    out_path = Path(asset.path)
    assert out_path.exists()

    # 30s source, default offset_s=60 > duration -> starts at 0.
    assert asset.tool_meta["start_s"] == 0.0

    probed = await extract.probe(out_path)
    streams = probed.payload["streams"]
    assert len(streams) == 1
    assert streams[0]["sample_rate"] == "16000"
    assert streams[0]["channels"] == 1


async def test_extract_embedded_subs_contains_expected_text(test_video: Path, tmp_path: Path) -> None:
    assets = await extract.extract_embedded_subs(test_video, tmp_path)
    assert len(assets) == 1
    sub_asset = assets[0]
    assert sub_asset.type == "subs"
    assert sub_asset.tool_meta["codec_name"] in extract.TEXT_SUB_CODECS
    assert sub_asset.tool_meta["stream_index"] == 0

    content = Path(sub_asset.path).read_text()
    assert "hello impostarr test subtitle" in content


async def test_sample_frames_returns_16_hashes_and_thumbnails(test_video: Path, tmp_path: Path) -> None:
    seq, assets = await extract.sample_frames(test_video, tmp_path, n=16)
    assert len(seq.hashes) == 16
    assert len(seq.timestamps) == 16
    assert len(assets) == 16
    assert seq.algo == "phash"

    for asset in assets:
        with Image.open(asset.path) as img:
            assert img.width <= 480


async def test_sample_frames_tool_meta_carries_timestamp_s(test_video: Path, tmp_path: Path) -> None:
    seq, assets = await extract.sample_frames(test_video, tmp_path, n=16)
    for asset, expected_ts in zip(assets, seq.timestamps, strict=True):
        assert asset.tool_meta["timestamp_s"] == expected_ts


async def test_sample_frames_deterministic_across_runs(test_video: Path, tmp_path: Path) -> None:
    seq_a, _ = await extract.sample_frames(test_video, tmp_path / "run1", n=16)
    seq_b, _ = await extract.sample_frames(test_video, tmp_path / "run2", n=16)
    assert seq_a.hashes == seq_b.hashes
    assert seq_a.timestamps == seq_b.timestamps


async def test_hamming_similarity_identical_is_one(test_video: Path, tmp_path: Path) -> None:
    seq, _ = await extract.sample_frames(test_video, tmp_path, n=16)
    assert extract.hamming_similarity(seq, seq) == 1.0


async def test_hamming_similarity_different_content_is_less_than_one(
    test_video: Path, test_video_alt: Path, tmp_path: Path
) -> None:
    seq_a, _ = await extract.sample_frames(test_video, tmp_path / "a", n=16)
    seq_b, _ = await extract.sample_frames(test_video_alt, tmp_path / "b", n=16)
    similarity = extract.hamming_similarity(seq_a, seq_b)
    assert similarity < 1.0


def test_hamming_similarity_empty_sequence_is_zero() -> None:
    empty = extract.FrameHashSeq(timestamps=[], hashes=[])
    non_empty = extract.FrameHashSeq(timestamps=[0.5], hashes=["0" * 16])
    assert extract.hamming_similarity(empty, non_empty) == 0.0
    assert extract.hamming_similarity(empty, empty) == 0.0


async def test_fingerprint_stable_and_changes_with_params(test_video: Path, tmp_path: Path) -> None:
    asset_a = await extract.extract_audio(test_video, tmp_path, offset_s=60.0, duration_s=900.0)
    asset_b = await extract.extract_audio(test_video, tmp_path, offset_s=60.0, duration_s=900.0)
    assert asset_a.input_fingerprint == asset_b.input_fingerprint

    asset_c = await extract.extract_audio(test_video, tmp_path, offset_s=5.0, duration_s=900.0)
    assert asset_c.input_fingerprint != asset_a.input_fingerprint


async def test_run_timeout_kills_process_and_raises() -> None:
    start = time.monotonic()
    with pytest.raises(extract.ExtractError, match="timed out"):
        await extract._run(["sleep", "5"], timeout_s=0.2)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # proves the process was killed, not waited out


async def test_extract_audio_probe_result_hint_avoids_extra_probe_call(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_result = await extract.probe(test_video)

    calls: list[Path] = []
    real_probe = extract.probe

    async def counting_probe(path: Path) -> extract.ExtractedAsset:
        calls.append(path)
        return await real_probe(path)

    monkeypatch.setattr(extract, "probe", counting_probe)

    asset = await extract.extract_audio(test_video, tmp_path, probe_result=probe_result)
    assert calls == []
    assert asset.path is not None
    assert Path(asset.path).exists()
