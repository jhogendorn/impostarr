"""A minimal, complete `IdentifierPlugin`.

Used by the loader tests to exercise entry-point discovery, and doubles as
a reference implementation for plugin authors: it always reports a single
candidate for the claimed episode with a fixed confidence, exactly the
contract's minimum requirement for a "yes/no" plugin (spec: "Yes/no plugins
return the single claimed-ident candidate.").
"""

from __future__ import annotations

from pydantic import BaseModel

from impostarr.plugins.base import (
    AssetBundle,
    Candidate,
    CandidateIdent,
    ClaimedIdent,
    IdentifierPlugin,
    PluginResult,
    SeriesContext,
)


class FakePluginConfig(BaseModel):
    confidence: float = 0.9


class FakePlugin(IdentifierPlugin):
    name = "fake"
    version = "1.0.0"
    config_model = FakePluginConfig

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        del assets, ctx
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=0.9,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )


class NoConfigFakePlugin(IdentifierPlugin):
    """An `IdentifierPlugin` with no `config_model` — exercises the loader's
    bare-instantiation path (`plugin_cls()`, no `config` kwarg)."""

    name = "no-config-fake"
    version = "1.0.0"

    async def identify(
        self, claimed: ClaimedIdent, assets: AssetBundle, ctx: SeriesContext
    ) -> PluginResult:
        del assets, ctx
        return PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=0.5,
                    ident=CandidateIdent(
                        series="claimed", season=claimed.season, episodes=claimed.episodes
                    ),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )


class BrokenFakePlugin:
    """Not a real plugin — used to simulate an entry point whose `load()`
    raises (e.g. a missing third-party dependency)."""

    def __init__(self) -> None:
        raise ImportError("simulated missing dependency")
