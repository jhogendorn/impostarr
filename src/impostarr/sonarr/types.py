"""Pydantic models for the subset of the Sonarr v3 API this project consumes.

Field names are snake_case with aliases matching Sonarr's camelCase JSON.
Only fields we actually read are modeled; everything else is ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_MODEL_CONFIG = ConfigDict(populate_by_name=True, extra="ignore")


class SystemStatus(BaseModel):
    model_config = _MODEL_CONFIG

    version: str


class HistoryRecord(BaseModel):
    """A single Sonarr history record (downloadFolderImported events only).

    Sonarr carries one `episodeId` per record (not an array); `episode_ids`
    normalizes that to a list for callers that want a uniform shape.
    `episode_file_id`, `guid`, and `indexer` live in the version-dependent
    `data` dict and are read defensively.
    """

    model_config = _MODEL_CONFIG

    id: int
    episode_id: int = Field(alias="episodeId")
    series_id: int = Field(alias="seriesId")
    source_title: str | None = Field(default=None, alias="sourceTitle")
    download_id: str | None = Field(default=None, alias="downloadId")
    date: datetime
    quality: dict[str, Any] = Field(default_factory=dict)
    languages: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def episode_ids(self) -> list[int]:
        return [self.episode_id]

    @property
    def episode_file_id(self) -> int | None:
        raw = self.data.get("fileId", self.data.get("episodeFileId"))
        return int(raw) if raw is not None else None

    @property
    def guid(self) -> str | None:
        return self.data.get("guid")

    @property
    def indexer(self) -> str | None:
        return self.data.get("indexer")


class EpisodeFile(BaseModel):
    model_config = _MODEL_CONFIG

    id: int
    series_id: int = Field(alias="seriesId")
    path: str
    size: int
    quality: dict[str, Any] = Field(default_factory=dict)
    languages: list[dict[str, Any]] = Field(default_factory=list)


class Series(BaseModel):
    model_config = _MODEL_CONFIG

    id: int
    title: str
    tvdb_id: int | None = Field(default=None, alias="tvdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    title_slug: str = Field(alias="titleSlug")


class Episode(BaseModel):
    model_config = _MODEL_CONFIG

    id: int
    season_number: int = Field(alias="seasonNumber")
    episode_number: int = Field(alias="episodeNumber")
    absolute_episode_number: int | None = Field(default=None, alias="absoluteEpisodeNumber")
    scene_season_number: int | None = Field(default=None, alias="sceneSeasonNumber")
    scene_episode_number: int | None = Field(default=None, alias="sceneEpisodeNumber")
    scene_absolute_episode_number: int | None = Field(
        default=None, alias="sceneAbsoluteEpisodeNumber"
    )
    episode_file_id: int = Field(alias="episodeFileId")
    has_file: bool = Field(alias="hasFile")


class ManualImportItem(BaseModel):
    model_config = _MODEL_CONFIG

    id: int | None = None
    path: str
    size: int | None = None
    series: dict[str, Any] | None = None
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)
    languages: list[dict[str, Any]] = Field(default_factory=list)
    rejections: list[dict[str, Any]] = Field(default_factory=list)
