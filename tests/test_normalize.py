from __future__ import annotations

from impostarr.normalize import (
    CrossSeriesCandidate,
    InSeriesCandidate,
    JunkCandidate,
    Unnormalizable,
    normalize,
)
from impostarr.plugins.base import (
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    ExternalIds,
    SeriesContext,
)

CLAIMED = ClaimedIdent(season=1, episodes=[1], episode_ids=[101])


def ep(
    id: int,
    season_number: int,
    episode_number: int,
    *,
    absolute_episode_number: int | None = None,
    scene_season_number: int | None = None,
    scene_episode_number: int | None = None,
) -> dict:
    return {
        "id": id,
        "season_number": season_number,
        "episode_number": episode_number,
        "absolute_episode_number": absolute_episode_number,
        "scene_season_number": scene_season_number,
        "scene_episode_number": scene_episode_number,
    }


def make_ctx(episodes: list[dict]) -> SeriesContext:
    return SeriesContext(series={"id": 1, "tvdbId": 42}, episodes=episodes, refsubs=None)


def candidate(
    *, season: int, episodes: list[int], numbering: str, series: str | ExternalIds = "claimed"
) -> Candidate:
    return Candidate(
        confidence=0.8,
        ident=CandidateIdent(series=series, season=season, episodes=episodes),
        numbering=numbering,
        evidence={},
    )


def test_tvdb_happy_path():
    ctx = make_ctx([ep(101, 1, 1), ep(102, 1, 2)])
    c = candidate(season=1, episodes=[2], numbering="tvdb")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({102}))


def test_absolute_ignores_season():
    ctx = make_ctx([ep(101, 1, 1, absolute_episode_number=1), ep(201, 2, 1, absolute_episode_number=13)])
    # candidate claims season 1, but absolute numbering should ignore that
    c = candidate(season=1, episodes=[13], numbering="absolute")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({201}))


def test_scene_with_fields_present():
    ctx = make_ctx(
        [ep(101, 1, 1, scene_season_number=1, scene_episode_number=5)]
    )
    c = candidate(season=1, episodes=[5], numbering="scene")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({101}))


def test_scene_fallback_to_plain_when_absent():
    ctx = make_ctx([ep(101, 1, 3)])  # no scene_* fields set
    c = candidate(season=1, episodes=[3], numbering="scene")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({101}))


def test_tmdb_treated_as_tvdb():
    ctx = make_ctx([ep(101, 1, 1), ep(102, 1, 2)])
    c = candidate(season=1, episodes=[2], numbering="tmdb")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({102}))


def test_multi_episode_union():
    ctx = make_ctx([ep(101, 1, 1), ep(102, 1, 2), ep(103, 1, 3)])
    c = candidate(season=1, episodes=[1, 2], numbering="tvdb")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({101, 102}))


def test_specials_season_zero():
    ctx = make_ctx([ep(901, 0, 1)])
    c = candidate(season=0, episodes=[1], numbering="tvdb")

    result = normalize(c, ctx, CLAIMED)

    assert result == InSeriesCandidate(episode_ids=frozenset({901}))


def test_unknown_number_unnormalizable():
    ctx = make_ctx([ep(101, 1, 1)])
    c = candidate(season=1, episodes=[99], numbering="tvdb")

    result = normalize(c, ctx, CLAIMED)

    assert isinstance(result, Unnormalizable)
    assert result.reason


def test_partial_multi_episode_match_is_unnormalizable_not_partial():
    ctx = make_ctx([ep(101, 1, 1)])
    c = candidate(season=1, episodes=[1, 99], numbering="tvdb")

    result = normalize(c, ctx, CLAIMED)

    assert isinstance(result, Unnormalizable)


def test_cross_series():
    ctx = make_ctx([ep(101, 1, 1)])
    c = candidate(season=1, episodes=[1], numbering="tvdb", series=ExternalIds(tvdb=999))

    result = normalize(c, ctx, CLAIMED)

    assert result == CrossSeriesCandidate(external_ids={"tvdb": 999})


def test_junk_ident_none():
    ctx = make_ctx([ep(101, 1, 1)])
    c = Candidate(confidence=0.6, ident=None, numbering=None, evidence={})

    result = normalize(c, ctx, CLAIMED)

    assert result == JunkCandidate()
