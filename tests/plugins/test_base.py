from __future__ import annotations

import pytest
from pydantic import ValidationError

from impostarr.plugins.base import (
    Candidate,
    CandidateIdent,
    ExternalIds,
    PluginResult,
)


def _claimed_candidate(**overrides) -> Candidate:
    defaults = {
        "confidence": 0.9,
        "ident": CandidateIdent(series="claimed", season=1, episodes=[1]),
        "numbering": "tvdb",
        "evidence": {},
    }
    defaults.update(overrides)
    return Candidate(**defaults)


def test_junk_candidate_has_no_numbering():
    candidate = Candidate(confidence=0.7, ident=None, numbering=None, evidence={})
    assert candidate.ident is None
    assert candidate.numbering is None


def test_numbering_without_ident_rejected():
    with pytest.raises(ValidationError, match="numbering must be None"):
        Candidate(confidence=0.5, ident=None, numbering="tvdb", evidence={})


def test_ident_without_numbering_rejected():
    with pytest.raises(ValidationError, match="numbering must be None"):
        Candidate(
            confidence=0.5,
            ident=CandidateIdent(series="claimed", season=1, episodes=[1]),
            numbering=None,
            evidence={},
        )


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        _claimed_candidate(confidence=1.1)
    with pytest.raises(ValidationError):
        _claimed_candidate(confidence=-0.1)
    # bounds inclusive
    _claimed_candidate(confidence=0.0)
    _claimed_candidate(confidence=1.0)


def test_external_ids_requires_at_least_one():
    with pytest.raises(ValidationError, match="at least one"):
        ExternalIds()
    ExternalIds(tvdb=123)
    ExternalIds(imdb="tt123")


def test_ok_status_requires_claimed_candidate():
    with pytest.raises(ValidationError, match="claimed"):
        PluginResult(
            status="ok",
            candidates=[
                Candidate(
                    confidence=0.5,
                    ident=CandidateIdent(series=ExternalIds(tvdb=9), season=1, episodes=[1]),
                    numbering="tvdb",
                    evidence={},
                )
            ],
        )


def test_ok_status_with_claimed_candidate_accepted():
    result = PluginResult(status="ok", candidates=[_claimed_candidate()])
    assert result.status == "ok"


def test_ok_status_requires_at_least_one_candidate():
    with pytest.raises(ValidationError, match="claimed"):
        PluginResult(status="ok", candidates=[])


def test_abstain_requires_reason():
    with pytest.raises(ValidationError, match="reason is required"):
        PluginResult(status="abstain")
    result = PluginResult(status="abstain", reason="no reference subtitles")
    assert result.reason == "no reference subtitles"


def test_error_requires_reason():
    with pytest.raises(ValidationError, match="reason is required"):
        PluginResult(status="error")
    result = PluginResult(status="error", reason="library raised")
    assert result.reason == "library raised"
