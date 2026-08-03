from __future__ import annotations

from pathlib import Path
from typing import Any

from impostarr.plugins.base import AssetBundle, ClaimedIdent, SeriesContext
from impostarr_plugin_whisper_subs import plugin as whisper_subs
from impostarr_plugin_whisper_subs.plugin import WhisperSubsConfig, WhisperSubsPlugin


class StubRefSubs:
    """Stand-in for `RefSubService`: async `get` keyed on (season, episode),
    returning a canned Path or None, and recording calls for cap assertions."""

    def __init__(self, mapping: dict[tuple[int, int], Path]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[int, int]] = []
        self.ext_ids_calls: list[dict[str, Any]] = []
        self.language_calls: list[str | None] = []

    async def get(
        self,
        series_ext_ids: dict[str, Any],
        season: int,
        episode: int,
        language: str | None = None,
    ) -> Path | None:
        self.calls.append((season, episode))
        self.ext_ids_calls.append(series_ext_ids)
        self.language_calls.append(language)
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


def test_series_ext_ids_includes_title() -> None:
    ext_ids = whisper_subs._series_ext_ids(make_series())

    assert ext_ids == {"tvdb": 123456, "title": "Show"}


def test_series_ext_ids_omits_title_when_blank() -> None:
    ext_ids = whisper_subs._series_ext_ids(make_series(title=""))

    assert "title" not in ext_ids


async def test_refsubs_get_called_with_title_in_series_ext_ids(tmp_path):
    e1_path = tmp_path / "S01E01.srt"
    write_srt(e1_path, ["a", "b", "c"])
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), [make_episode(1, 1)], refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=make_transcript(["a", "b", "c"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    await plugin.identify(claimed, assets, ctx)

    assert refsubs.ext_ids_calls
    assert all(ext_ids == {"tvdb": 123456, "title": "Show"} for ext_ids in refsubs.ext_ids_calls)


async def test_refsubs_get_called_with_transcript_language(tmp_path):
    e1_path = tmp_path / "S01E01.srt"
    write_srt(e1_path, ["a", "b", "c"])
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), [make_episode(1, 1)], refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    transcript = make_transcript(["a", "b", "c"])
    transcript["language"] = "ja"
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    await plugin.identify(claimed, assets, ctx)

    assert refsubs.language_calls == ["ja"]


async def test_refsubs_get_called_with_none_when_transcript_language_blank(tmp_path):
    e1_path = tmp_path / "S01E01.srt"
    write_srt(e1_path, ["a", "b", "c"])
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), [make_episode(1, 1)], refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    transcript = make_transcript(["a", "b", "c"])
    transcript["language"] = ""
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    await plugin.identify(claimed, assets, ctx)

    assert refsubs.language_calls == [None]


async def test_evidence_records_refsub_language_from_path_suffix(tmp_path):
    e1_path = tmp_path / "S01E01.ja.srt"
    write_srt(e1_path, ["a", "b", "c"])
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), [make_episode(1, 1)], refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    transcript = make_transcript(["a", "b", "c"])
    transcript["language"] = "ja"
    assets = AssetBundle(transcript=transcript)

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert result.candidates[0].evidence["refsub_language"] == "ja"


async def test_evidence_refsub_language_none_for_legacy_unsuffixed_path(tmp_path):
    e1_path = tmp_path / "S01E01.srt"
    write_srt(e1_path, ["a", "b", "c"])
    refsubs = StubRefSubs({(1, 1): e1_path})
    ctx = make_ctx(make_series(), [make_episode(1, 1)], refsubs)
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=make_transcript(["a", "b", "c"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert result.candidates[0].evidence["refsub_language"] is None


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


# NOTE: this used to assert status="ok" with a fabricated confidence=0.0
# candidate for the claimed episode (evidence note "no reference subs for
# claimed"). Production data (job 14, American Dad S05E07) showed this
# exact shape poisoning aggregate scoring: a 0.0 the plugin never actually
# measured read downstream as "certain non-match" and dragged s_claimed
# into quarantine even though other plugins agreed on a high-confidence
# match. Fixed to abstain instead -- see plugin.py's "Evidence integrity"
# docstring section. Updated (not deleted) per instruction to document the
# change rather than silently drop the regression coverage.
async def test_claimed_without_subs_but_others_compared_abstains(tmp_path):
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

    assert result.status == "abstain"
    assert result.reason == "no reference subtitles for the claimed episode"
    assert result.candidates == []


async def test_claimed_refsub_present_but_scores_zero_stays_ok(tmp_path, monkeypatch):
    """Claimed episode's refsub IS available (measured), but comparison
    legitimately yields 0.0 (garbage/unmatchable reference content) -- this
    is a real measurement, not an abstention, and must be reported as-is.
    `_match_ratio` is forced to 0.0 rather than relied on to land there
    naturally, since fuzzy string matching can return small nonzero ratios
    even for unrelated content -- this test is about the plugin's handling
    of a genuine zero score, not about crafting adversarial fuzz input."""
    e17_path = tmp_path / "S01E17.srt"
    write_srt(e17_path, ["asdf qwer zxcv poiu", "lkjh mnbv trew yuio"])
    transcript = make_transcript(["completely different actual dialogue here"])

    episodes = [make_episode(1, 17)]
    refsubs = StubRefSubs({(1, 17): e17_path})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[17])
    assets = AssetBundle(transcript=transcript)

    monkeypatch.setattr(whisper_subs, "_match_ratio", lambda segments, srt_lines: (0.0, len(srt_lines)))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=1, min_compared=1))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    by_ep = {c.ident.episodes[0]: c for c in result.candidates}
    assert by_ep[17].confidence == 0.0
    assert by_ep[17].evidence["refsub_path"] == str(e17_path)
    assert "note" not in by_ep[17].evidence


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

    # min_compared=3 matches the SRT's line count, so the thin-refsub
    # discount (tested separately) doesn't confound this markup assertion.
    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=3, min_compared=3))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    assert result.candidates[0].confidence > 0.9


async def test_thin_refsub_discounts_confidence(tmp_path):
    pool = [
        "the crew boards the ship at dawn",
        "the captain gives the final order",
        "engines ignite and they depart quietly",
        "silence fills the bridge",
        "stars streak past the viewport",
        "the navigator checks the coordinates",
        "warning lights flicker on the console",
        "the first officer reports all clear",
        "the ship enters hyperspace smoothly",
        "the crew breathes a collective sigh",
    ]
    thin_path = tmp_path / "thin.srt"
    full_path = tmp_path / "full.srt"
    write_srt(thin_path, pool[:2])
    write_srt(full_path, pool)
    transcript = make_transcript(pool)

    episodes = [make_episode(1, 1)]
    claimed = make_claimed(season=1, episodes=[1])
    assets = AssetBundle(transcript=transcript)
    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=5, min_compared=10))

    thin_ctx = make_ctx(make_series(), episodes, StubRefSubs({(1, 1): thin_path}))
    full_ctx = make_ctx(make_series(), episodes, StubRefSubs({(1, 1): full_path}))

    thin_result = await plugin.identify(claimed, assets, thin_ctx)
    full_result = await plugin.identify(claimed, assets, full_ctx)

    assert thin_result.status == "ok"
    assert full_result.status == "ok"
    thin_candidate = thin_result.candidates[0]
    full_candidate = full_result.candidates[0]

    assert thin_candidate.confidence < full_candidate.confidence
    assert thin_candidate.evidence["thin_refsub_discount"] < 1.0
    assert full_candidate.evidence["thin_refsub_discount"] == 1.0


async def test_refsub_fetches_run_concurrently_and_stay_ordered(tmp_path):
    srt_path = tmp_path / "shared.srt"
    write_srt(srt_path, ["shared content line"])
    episodes = [make_episode(1, n) for n in range(15, 21)]
    refsubs = StubRefSubs({(1, n): srt_path for n in range(15, 21)})
    ctx = make_ctx(make_series(), episodes, refsubs)
    claimed = make_claimed(season=1, episodes=[17])
    assets = AssetBundle(transcript=make_transcript(["shared content line", "extra"]))

    plugin = WhisperSubsPlugin(WhisperSubsConfig(min_lines=2))
    result = await plugin.identify(claimed, assets, ctx)

    assert result.status == "ok"
    # Deterministic: candidates sorted by confidence, ties broken by the
    # original distance-sorted (season_episodes) order, not fetch-completion
    # order — with identical content every episode ties, so identifying the
    # exact set is what matters here.
    assert {c.ident.episodes[0] for c in result.candidates} == {15, 16, 17, 18, 19, 20}


# -- parse_srt edge cases ----------------------------------------------------


def test_parse_srt_strips_leading_bom():
    text = "﻿1\n00:00:01,000 --> 00:00:02,000\nHello there\n"
    assert whisper_subs.parse_srt(text) == ["Hello there"]


def test_parse_srt_handles_crlf_line_endings():
    text = "1\r\n00:00:01,000 --> 00:00:02,000\r\nHello there\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nSecond line\r\n"
    assert whisper_subs.parse_srt(text) == ["Hello there", "Second line"]


def test_parse_srt_skips_malformed_cue_without_arrow():
    text = "1\nnot a timestamp\nGhost line\n\n2\n00:00:03,000 --> 00:00:04,000\nReal line\n"
    assert whisper_subs.parse_srt(text) == ["Real line"]


def test_parse_srt_handles_missing_index_line():
    text = "00:00:01,000 --> 00:00:02,000\nHello there\n"
    assert whisper_subs.parse_srt(text) == ["Hello there"]
