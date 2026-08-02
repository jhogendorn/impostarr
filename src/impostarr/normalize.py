"""Normalize plugin candidates to Sonarr episode ids.

Pure, no I/O. `ctx.episodes` is assumed to be a list of `Episode.model_dump()`
dicts (see `impostarr.sonarr.types.Episode`) — field names are therefore
snake_case (`season_number`, `episode_number`, `absolute_episode_number`,
`scene_season_number`, `scene_episode_number`, `id`), not Sonarr's raw
camelCase JSON keys.

Mapping rules (spec "Plugin contract" / "Core normalizes..."):
- `ident is None` -> junk (negative evidence, no ident to map).
- `ident.series != "claimed"` -> cross-series (external ids carried through,
  no episode mapping attempted).
- numbering "tvdb" -> match each episode number against
  `(season_number, episode_number)`.
- numbering "tmdb" -> treated as tvdb standard order (documented limitation:
  no separate tmdb-ordered episode list is available to map against).
- numbering "absolute" -> match against `absolute_episode_number`; the
  candidate's `season` is ignored (absolute numbering is season-independent).
- numbering "scene" -> three-tier precedence per episode number: exact
  `(scene_season_number, scene_episode_number)` match first; then
  `scene_absolute_episode_number` (season-independent, like absolute
  numbering); then plain `(season_number, episode_number)` as the final
  fallback. See `_match_scene` for why this replaced a naive per-field
  fallback.
- Multi-episode idents: every number in `ident.episodes` must resolve to an
  episode id; the result is the union of the resolved ids. If any number
  fails to resolve, the whole candidate is `Unnormalizable` (never a partial
  match).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .plugins.base import Candidate, ClaimedIdent, SeriesContext

EpisodeDict = dict[str, Any]


class InSeriesCandidate(BaseModel):
    kind: Literal["in_series"] = "in_series"
    episode_ids: frozenset[int]


class CrossSeriesCandidate(BaseModel):
    kind: Literal["cross_series"] = "cross_series"
    external_ids: dict[str, Any]


class JunkCandidate(BaseModel):
    kind: Literal["junk"] = "junk"


NormalizedCandidate = InSeriesCandidate | CrossSeriesCandidate | JunkCandidate


class Unnormalizable(BaseModel):
    reason: str


def _match_tvdb(episodes: list[EpisodeDict], season: int, epnum: int) -> int | None:
    for ep in episodes:
        if ep.get("season_number") == season and ep.get("episode_number") == epnum:
            return ep["id"]
    return None


def _match_absolute(episodes: list[EpisodeDict], season: int, epnum: int) -> int | None:
    del season  # absolute numbering is season-independent; ignored per spec
    for ep in episodes:
        if ep.get("absolute_episode_number") == epnum:
            return ep["id"]
    return None


def _match_scene(episodes: list[EpisodeDict], season: int, epnum: int) -> int | None:
    """Three-tier precedence, each tier a full pass over `episodes`:

    1. `(scene_season_number, scene_episode_number) == (season, epnum)`.
    2. `scene_absolute_episode_number == epnum` (season-independent).
    3. plain `(season_number, episode_number) == (season, epnum)`.

    Earlier tiers were a per-field independent fallback (missing
    scene_season_number fell back to season_number independently of
    scene_episode_number), but TheXEM populates the scene season/episode
    pair together in practice — it's never partially set — so an explicit
    three-tier precedence is both more correct and easier to reason about
    than substituting fields individually.
    """
    for ep in episodes:
        if ep.get("scene_season_number") == season and ep.get("scene_episode_number") == epnum:
            return ep["id"]
    for ep in episodes:
        if ep.get("scene_absolute_episode_number") == epnum:
            return ep["id"]
    for ep in episodes:
        if ep.get("season_number") == season and ep.get("episode_number") == epnum:
            return ep["id"]
    return None


_MATCHERS = {
    "tvdb": _match_tvdb,
    "tmdb": _match_tvdb,
    "absolute": _match_absolute,
    "scene": _match_scene,
}


def normalize(
    candidate: Candidate, ctx: SeriesContext, claimed: ClaimedIdent
) -> NormalizedCandidate | Unnormalizable:
    del claimed  # not needed by any mapping rule; kept for interface symmetry

    ident = candidate.ident
    if ident is None:
        return JunkCandidate()

    if ident.series != "claimed":
        return CrossSeriesCandidate(external_ids=ident.series.model_dump(exclude_none=True))

    matcher = _MATCHERS[candidate.numbering]
    episode_ids: set[int] = set()
    for epnum in ident.episodes:
        matched_id = matcher(ctx.episodes, ident.season, epnum)
        if matched_id is None:
            return Unnormalizable(
                reason=(
                    f"no episode found for season={ident.season} episode={epnum} "
                    f"numbering={candidate.numbering!r}"
                )
            )
        episode_ids.add(matched_id)

    return InSeriesCandidate(episode_ids=frozenset(episode_ids))
