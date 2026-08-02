"""Session-scoped ffmpeg-generated test video fixtures for asset extraction
tests. Each video is built once per test session (ffmpeg generation is slow
relative to the extraction calls under test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

DURATION_S = 30

SRT_CONTENT = """1
00:00:01,000 --> 00:00:05,000
hello impostarr test subtitle

2
00:00:10,000 --> 00:00:15,000
second line of dialogue
"""


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg fixture generation failed: {proc.stderr}")


@pytest.fixture(scope="session")
def test_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ~30s mkv: testsrc2 video, sine audio, one embedded SRT subtitle stream."""
    tmp_dir = tmp_path_factory.mktemp("assets_fixture")
    srt_path = tmp_dir / "subs.srt"
    srt_path.write_text(SRT_CONTENT)
    video_path = tmp_dir / "sample.mkv"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=duration={DURATION_S}:size=640x360:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={DURATION_S}",
            "-i",
            str(srt_path),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-c:s",
            "srt",
            str(video_path),
        ]
    )
    return video_path


@pytest.fixture(scope="session")
def test_video_alt(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A second ~30s mkv, visually different from `test_video` (SMPTE color
    bars vs. testsrc2), no subtitle stream — used for hamming_similarity's
    "different content" case."""
    tmp_dir = tmp_path_factory.mktemp("assets_fixture_alt")
    video_path = tmp_dir / "sample_alt.mkv"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"smptebars=duration={DURATION_S}:size=640x360:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=880:duration={DURATION_S}",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video_path),
        ]
    )
    return video_path
