"""Identifier plugin contract: pydantic models + abstract base class.

Mirrors the spec's "Plugin contract" section exactly. A plugin receives
`(claimed, assets, ctx)` and returns a `PluginResult`.

Claimed-ident validation note: `PluginResult`'s `ok` validator only checks
that *some* candidate has `ident.series == "claimed"` — i.e. references the
claimed series at all. It cannot check that candidate's season/episodes
match the actual claimed season/episode numbers, because the contract
models (this module) have no access to the `ClaimedIdent` the plugin was
called with; that stronger check ("is there a candidate for the *exact*
claimed episode") is a caller-side concern, not a model validator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExternalIds(BaseModel):
    """Cross-database series identifiers; at least one must be set."""

    tvdb: int | None = None
    tmdb: int | None = None
    imdb: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ExternalIds:
        if self.tvdb is None and self.tmdb is None and self.imdb is None:
            raise ValueError("ExternalIds requires at least one of tvdb/tmdb/imdb")
        return self


class CandidateIdent(BaseModel):
    series: Literal["claimed"] | ExternalIds
    season: int
    episodes: list[int] = Field(min_length=1)


class Candidate(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    ident: CandidateIdent | None
    numbering: Literal["tvdb", "tmdb", "absolute", "scene"] | None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _numbering_ident_coupling(self) -> Candidate:
        if (self.ident is None) != (self.numbering is None):
            raise ValueError("numbering must be None if and only if ident is None")
        return self


class PluginResult(BaseModel):
    status: Literal["ok", "abstain", "error"]
    reason: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> PluginResult:
        if self.status in ("abstain", "error") and not self.reason:
            raise ValueError(f"reason is required when status={self.status!r}")
        if self.status == "ok" and not any(
            c.ident is not None and c.ident.series == "claimed" for c in self.candidates
        ):
            raise ValueError(
                "status 'ok' requires at least one candidate with ident.series == 'claimed'"
            )
        return self


class ClaimedIdent(BaseModel):
    """The episode Sonarr claims the file is, as passed to a plugin."""

    season: int
    episodes: list[int] = Field(min_length=1)
    episode_ids: list[int] = Field(min_length=1)


class AssetBundle(BaseModel):
    """Cached extraction artifacts for a file, all optional (extraction is
    best-effort; plugins abstain when what they need is missing)."""

    probe: dict[str, Any] | None = None
    audio_path: str | None = None
    transcript: dict[str, Any] | None = None
    sub_paths: list[str] = Field(default_factory=list)
    frame_hashes: dict[str, Any] | None = None


class SeriesContext(BaseModel):
    """Sonarr series/episode context plus the reference subtitle service.

    `refsubs` holds a `RefSubService` instance; typed `Any` here (rather
    than importing that module) to avoid a plugins -> refsubs -> plugins
    import cycle risk, and excluded from serialization since it isn't
    data.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    series: dict[str, Any]
    episodes: list[dict[str, Any]]
    refsubs: Any = Field(default=None, exclude=True)


class IdentifierPlugin(ABC):
    """Base class for identifier plugins, discovered via the
    `impostarr.identifiers` entry-point group.

    Constructor accepts an optional validated `config` (an instance of the
    subclass's `config_model`), stored on `self.config`. The loader
    (`plugins/loader.py`) passes it through when `config_model` is set;
    plugins with no `config_model` are instantiated bare (`self.config`
    stays `None`). Subclasses that want a non-`None` default may override
    `__init__` and call `super().__init__(config or MyConfig())`.
    """

    name: str
    version: str
    config_model: type[BaseModel] | None = None

    def __init__(self, config: BaseModel | None = None) -> None:
        self.config = config

    @abstractmethod
    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult: ...
