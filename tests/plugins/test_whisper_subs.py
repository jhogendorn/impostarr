from __future__ import annotations

from pathlib import Path
from typing import Any

from impostarr.plugins import whisper_subs
from impostarr.plugins.base import AssetBundle, ClaimedIdent, SeriesContext
from impostarr.plugins.whisper_subs import WhisperSubsConfig, WhisperSubsPlugin


class StubRefSubs:
    """Stand-in for `RefSubService`: async `get` keyed on (season, episode),
    returning a canned Path or None, and recording calls for cap assertions."""

    def __init__(self, mapping: dict[tuple[int, int], Path]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[int, int]] = []

    async def get(self, series_ext_ids: dict[str, Any], season: int, episode: int) -> Path | None:
        self.calls.append((season, episode))
        return self.mapping.get((season, episode))


def make_series(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "title": "Show",
        "tvdb_id": 123456,
        "tmdb_id": None,
        "imdb_id": None,
        "title_slug": "show",
    }
    base.update(overrides)
    return base


def make_episode(season: int, episode: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": season * 1000 + episode,
        "season_number": season,
        "episode_number": episode,
        "absolute_episode_number": None,
        "scene_season_number": None,
        "scene_episode_number": None,
        "scene_absolute_episode_number": None,
        "episode_file_id": 0,
        "has_file": True,
    }
    base.update(overrides)
    return base


def make_claimed(season: int, episodes: list[int]) -> ClaimedIdent:
    return ClaimedIdent(season=season, episodes=episodes, episode_ids=[1])


def make_transcript(lines: list[str]) -> dict[str, Any]:
    return {
        "segments": [
            {"start": i * 2.0, "end": i * 2.0 + 1.5, "text": line} for i, line in enumerate(lines)
        ],
        "language": "en",
    }


def write_srt(path: Path, lines: list[str]) -> None:
    blocks = []
    for i, line in enumerate(lines, start=1):
        blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{line}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks))


def make_ctx(series: dict[str, Any], episodes: list[dict[str, Any]], refsubs: Any) -> SeriesContext:
    return SeriesContext(series=series, episodes=episodes, refsubs=refsubs)


async def test_correct_ranked_candidates_with_mislabel(tmp_path):
    e17_path = tmp_path / "S01E17.srt"
    e18_path = tmp_path / "S01E18.srt"
    write_srt(
        e17_path,
        [
            "zebra quokka narwhal xylophone",
            "unrelated goose parade tuba",
            "moonlit turnip festival banjo",
        ],
    )
    write_srt(
        e18_path,
        [
            "the crew boards the ship at dawn",
            "the captain gives the final order",
            "engines ignite and they depart quietly",
        ],
    )
    # transcript actually matches E18's content (mislabeled file claims E17)
    transcript = make_transcript(
        [
            "the crew boards the ship at dawn",
            "the captain gives the final order",
            "engines ignite and they depart quietly",
        ]
    )

    episodes = [make_episode(1, n) for n in range(15, 21)]
    refsubs = StubRefSubs({(1, 17): e17_path, (1, 18): e18_path})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[17])
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    by_ep = {c.ident.episodes[0]: c for c in result.candidates}
    assert set(by_ep) == {17, 18}
    assert by_ep[18].confidence > by_ep[17].confidence
    assert by_ep[18].ident.series == "claimed"
    assert by_ep[18].numbering == "tvdb"
    assert by_ep[18].evidence["refsub_path"] == str(e18_path)


async def test_abstain_when_no_transcript(tmp_path):
    ctx = make_ctx(make_series(), [make_episode(1, 1)], StubRefSubs({}))
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=None)

    plugin = WhisperSubsPlugin()
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "no transcript"


async def test_abstain_when_transcript_shorter_than_min_lines(tmp_path):
    ctx = make_ctx(make_series(), [make_episode(1, 1)], StubRefSubs({}))
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=make_transcript(["one", "two", "three"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=20))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "transcript too short"


async def test_abstain_when_zero_episodes_have_reference_subs(tmp_path):
    episodes = [make_episode(1, n) for n in range(1, 4)]
    refsubs = StubRefSubs({})  # nothing has subs
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=make_transcript(["a", "b", "c"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "abstain"
    assert result.reason == "no reference subtitles"


async def test_claimed_without_subs_but_others_compared(tmp_path):
    e16_path = tmp_path / "S01E16.srt"
    e18_path = tmp_path / "S01E18.srt"
    write_srt(e16_path, ["some content here", "more lines follow"])
    write_srt(e18_path, ["other content entirely", "different lines here"])
    transcript = make_transcript(["some content here", "more lines follow"])

    episodes = [make_episode(1, n) for n in range(15, 20)]
    # claimed episode 17 has NO reference subs; 16 and 18 do
    refsubs = StubRefSubs({(1, 16): e16_path, (1, 18): e18_path})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[17])
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=2))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    by_ep = {c.ident.episodes[0]: c for c in result.candidates}
    assert set(by_ep) == {16, 17, 18}
    assert by_ep[17].confidence == 0.0
    assert "no reference subs for claimed" in by_ep[17].evidence.get("note", "")


async def test_library_exception_path_returns_error(tmp_path, monkeypatch):
    e1_path = tmp_path / "S01E01.srt"
    write_srt(e1_path, ["some content here"])
    episodes = [make_episode(1, 1)]
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=make_transcript(["some content here", "extra"]))

    def boom(*args, **kwargs):
        raise RuntimeError("comparison internals blew up")

    monkeypatch.setattr(whisper_subs, "_match_ratio", boom)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=2))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "error"
    assert result.reason


async def test_candidate_window_capped_at_ten_nearest(tmp_path):
    srt_path = tmp_path / "shared.srt"
    write_srt(srt_path, ["shared content line"])
    episodes = [make_episode(1, n) for n in range(1, 31)]  # 30-episode season
    refsubs = StubRefSubs({(1, n): srt_path for n in range(1, 31)})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[15])
    assets = AssetBundle(transcript=make_transcript(["shared content line", "extra"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=2))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert len(refsubs.calls) <= 11
    assert len(result.candidates) <= 11


def test_normalize_strips_html_tags_before_punctuation():
    assert whisper_subs._normalize("<i>Hello there</i>") == whisper_subs._normalize("Hello there")


def test_normalize_strips_ass_override_blocks_before_punctuation():
    assert whisper_subs._normalize(r"{\an8}Hello there") == whisper_subs._normalize("Hello there")


async def test_match_ratio_high_for_markup_srt_vs_clean_transcript(tmp_path):
    tagged_path = tmp_path / "S01E01.srt"
    write_srt(
        tagged_path,
        [
            r"{\an8}<i>The crew boards the ship at dawn</i>",
            "<b>The captain gives the final order</b>",
            r"{\an8}Engines ignite and they depart quietly",
        ],
    )
    transcript = make_transcript(
        [
            "The crew boards the ship at dawn",
            "The captain gives the final order",
            "Engines ignite and they depart quietly",
        ]
    )
    episodes = [make_episode(1, 1)]
    refsubs = StubRefSubs({(1, 1): tagged_path})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert result.candidates[0].confidence > 0.9
