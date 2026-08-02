from __future__ import annotations

import pytest

from impostarr.config import Thresholds
from impostarr.normalize import (
    CrossSeriesCandidate,
    InSeriesCandidate,
    JunkCandidate,
    NormalizedCandidate,
    Unnormalizable,
)
from impostarr.plugins.base import Candidate, CandidateIdent, PluginResult
from impostarr.scoring import (
    InstanceFlags,
    PluginOutcome,
    Remap,
    Replace,
    aggregate,
    route,
)

CLAIMED = frozenset({10})
ALT = frozenset({11})

# Placeholder raw ident/numbering: aggregate() consumes the pre-computed
# `normalized` list, not the raw candidate.ident/numbering, so these are
# fixed dummies that only need to satisfy Candidate/PluginResult validators.
_DUMMY_IDENT = CandidateIdent(series="claimed", season=1, episodes=[1])


def _candidate(confidence: float) -> Candidate:
    return Candidate(confidence=confidence, ident=_DUMMY_IDENT, numbering="tvdb")


def outcome(
    name: str,
    weight: float,
    *,
    status: str = "ok",
    pairs: list[tuple[float, NormalizedCandidate | Unnormalizable]] = (),
    reason: str | None = None,
) -> PluginOutcome:
    """Build a PluginOutcome. `pairs` is (confidence, normalized) aligned by index."""
    pairs = list(pairs)
    candidates = [_candidate(c) for c, _ in pairs] if status == "ok" else []
    normalized = [n for _, n in pairs] if status == "ok" else []
    result = PluginResult(status=status, reason=reason, candidates=candidates)
    return PluginOutcome(name, weight, result, normalized)


def IN(episode_ids: frozenset[int]) -> InSeriesCandidate:
    return InSeriesCandidate(episode_ids=episode_ids)


CROSS = CrossSeriesCandidate(external_ids={"tvdb": 5000})
JUNK = JunkCandidate()

DEFAULT_THRESHOLDS = Thresholds()  # quarantine=0.8 auto=0.4 alt=0.8 alt_margin=0.2 auto_min_evidence=2
FLAGS_ON = InstanceFlags(auto_remap=True, auto_replace=True)
FLAGS_OFF = InstanceFlags(auto_remap=False, auto_replace=False)
FLAGS_ON_APPROVAL_REQUIRED = InstanceFlags(auto_remap=True, auto_replace=True, approval_required=True)


# ---------------------------------------------------------------------------
# aggregate() mechanics
# ---------------------------------------------------------------------------


def test_aggregate_all_abstain_empty_applicable():
    outcomes = [
        outcome("p1", 1.0, status="abstain", reason="no subs"),
        outcome("p2", 1.0, status="abstain", reason="no llm"),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed is None
    assert sheet.s_alt is None
    assert sheet.applicable_count == 0
    assert sheet.per_candidate == {}


def test_aggregate_all_error_empty_applicable():
    outcomes = [
        outcome("p1", 1.0, status="error", reason="crashed"),
        outcome("p2", 1.0, status="error", reason="timeout"),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed is None
    assert sheet.applicable_count == 0


def test_aggregate_weight_asymmetry_higher_weight_wins():
    outcomes = [
        outcome("heavy", 3.0, pairs=[(0.9, IN(CLAIMED))]),
        outcome("light", 1.0, pairs=[(0.1, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.7)  # (3*0.9 + 1*0.1) / 4


def test_aggregate_weight_asymmetry_reversed():
    outcomes = [
        outcome("heavy", 3.0, pairs=[(0.1, IN(CLAIMED))]),
        outcome("light", 1.0, pairs=[(0.9, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.3)  # (3*0.1 + 1*0.9) / 4


def test_aggregate_all_zero_weight_collapses_scores_to_zero():
    # Deliberately pinning current behavior: when every reporting plugin's
    # weight is 0.0, sum(weight) for a key is 0, and the guard against
    # division-by-zero defaults that key's score to 0.0 - regardless of how
    # high the reported confidences were. Config now rejects negative
    # weights (see test_config.py), but 0.0 is a valid weight (an
    # effectively-disabled-but-still-applicable plugin) and this is its
    # documented scoring behavior.
    outcomes = [
        outcome("p1", 0.0, pairs=[(0.99, IN(CLAIMED))]),
        outcome("p2", 0.0, pairs=[(0.99, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.applicable_count == 2
    assert sheet.s_claimed == 0.0
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    # s_claimed=0.0 < auto threshold -> remediation path; no alt reported.
    assert decision.outcome == "remediate"
    assert isinstance(decision.action, Replace)


def test_aggregate_same_key_dedupe_takes_max_within_plugin():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.3, IN(CLAIMED)), (0.9, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.9)


def test_aggregate_claimed_candidate_normalized_elsewhere_scores_zero_and_alt():
    # Plugin's claimed-series candidate normalized to a DIFFERENT episode set
    # (ALT, not CLAIMED) -> contributes 0.0 to the claimed key, and its actual
    # key (ALT) scores at its own reported confidence.
    outcomes = [outcome("p1", 1.0, pairs=[(0.8, IN(ALT))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.0)
    assert sheet.s_alt == pytest.approx(0.8)
    assert sheet.alt_key == ALT
    assert sheet.alt_kind == "in_series"


def test_aggregate_unnormalizable_excluded():
    outcomes = [
        outcome(
            "p1",
            1.0,
            pairs=[(0.8, IN(CLAIMED)), (0.5, Unnormalizable(reason="no such episode"))],
        )
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.8)
    assert sheet.s_alt is None  # only the unnormalizable non-claimed candidate existed


def test_aggregate_alt_is_max_across_non_claimed_keys():
    other_alt = frozenset({12})
    outcomes = [
        outcome(
            "p1",
            1.0,
            pairs=[(0.1, IN(CLAIMED)), (0.5, IN(ALT)), (0.7, IN(other_alt))],
        )
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt == pytest.approx(0.7)
    assert sheet.alt_key == other_alt


def test_aggregate_claimed_vs_alt_denominator_asymmetry():
    # CRITICAL invariant: the claimed key's denominator always includes every
    # applicable plugin (via the 0.0-injection rule), but a non-claimed key's
    # denominator only includes plugins that actually reported it. p2 doesn't
    # report ALT at all, so s_alt must be p1's raw 0.9 - NOT averaged/halved
    # against p2's weight as if p2 had implicitly reported 0.0 there too.
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.075)  # (1*0.1 + 1*0.05) / 2
    assert sheet.s_alt == pytest.approx(0.9)  # (1*0.9) / 1 - p2 never reported ALT


def test_aggregate_alt_tie_break_is_deterministic():
    tied_a = frozenset({11})
    tied_b = frozenset({12})
    outcomes = [
        outcome(
            "p1",
            1.0,
            pairs=[(0.1, IN(CLAIMED)), (0.7, IN(tied_a)), (0.7, IN(tied_b))],
        )
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt == pytest.approx(0.7)
    assert sheet.alt_key == tied_a  # "in_series:11" sorts before "in_series:12"


def test_aggregate_cross_series_key_and_kind():
    outcomes = [outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, CROSS)])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt == pytest.approx(0.9)
    assert sheet.alt_kind == "cross_series"


def test_aggregate_junk_key_and_kind():
    outcomes = [outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, JUNK)])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt == pytest.approx(0.9)
    assert sheet.alt_kind == "junk"


def test_aggregate_abstain_and_error_excluded_from_scoring():
    outcomes = [
        outcome("ok_plugin", 1.0, pairs=[(0.9, IN(CLAIMED))]),
        outcome("abstainer", 100.0, status="abstain", reason="no subs"),
        outcome("errorer", 100.0, status="error", reason="crashed"),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.applicable_count == 1
    assert sheet.s_claimed == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# route() — table-driven over outcome/action/auto
# ---------------------------------------------------------------------------


def test_route_inconclusive_all_abstain():
    outcomes = [outcome("p1", 1.0, status="abstain", reason="no subs")]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "inconclusive"
    assert decision.action is None
    assert decision.auto is False
    assert "no applicable evidence" in decision.reason


def test_route_inconclusive_all_error():
    outcomes = [outcome("p1", 1.0, status="error", reason="crashed")]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "inconclusive"
    assert decision.action is None


def test_route_matched_single_plugin_below_min_evidence():
    # min-evidence gates auto REMEDIATION only, not matching.
    outcomes = [outcome("p1", 1.0, pairs=[(0.95, IN(CLAIMED))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.applicable_count == 1 < DEFAULT_THRESHOLDS.auto_min_evidence
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "matched"
    assert decision.action is None
    assert decision.auto is False


def test_route_mid_band_quarantine():
    outcomes = [outcome("p1", 1.0, pairs=[(0.6, IN(CLAIMED))])]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "quarantine"
    assert decision.action is None
    assert decision.auto is False


def test_route_low_credible_same_series_alt_both_flags_on_remediate_remap():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, IN(ALT))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.applicable_count == 2
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "remediate"
    assert decision.auto is True
    assert isinstance(decision.action, Remap)
    assert decision.action.target_episode_ids == ALT


def test_route_low_credible_same_series_alt_auto_remap_off_quarantine_proposed():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, IN(ALT))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    flags = InstanceFlags(auto_remap=False, auto_replace=True)
    decision = route(sheet, DEFAULT_THRESHOLDS, flags)
    assert decision.outcome == "quarantine"
    assert decision.auto is False
    assert isinstance(decision.action, Remap)
    assert decision.action.target_episode_ids == ALT


def test_route_low_credible_cross_series_alt_replace():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, CROSS)]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, CROSS)]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "remediate"
    assert decision.auto is True
    assert isinstance(decision.action, Replace)


def test_route_low_credible_cross_series_alt_auto_replace_off_quarantine_proposed():
    # Pins the replace-side flag gate, mirroring the remap-side test above:
    # auto_replace off -> proposed Replace stays in quarantine, not remediate.
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, CROSS)]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, CROSS)]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    flags = InstanceFlags(auto_remap=True, auto_replace=False)
    decision = route(sheet, DEFAULT_THRESHOLDS, flags)
    assert decision.outcome == "quarantine"
    assert decision.auto is False
    assert isinstance(decision.action, Replace)


def test_route_low_credible_junk_replace():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, JUNK)]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, JUNK)]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "remediate"
    assert isinstance(decision.action, Replace)


def test_route_low_no_alt_replace():
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED))]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt is None
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "remediate"
    assert isinstance(decision.action, Replace)


def test_route_low_alt_below_alt_threshold_replace_not_remap():
    # s_alt (0.5) is below thresholds.alt (0.8): not credible even though
    # alt_kind is in_series and margin would otherwise pass.
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.5, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.5, IN(ALT))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_alt == pytest.approx(0.5)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert isinstance(decision.action, Replace)


def test_route_low_alt_margin_failure_replace_not_remap():
    # s_alt clears thresholds.alt but the margin over s_claimed is too small:
    # credible requires BOTH conditions, so this falls to Replace.
    thresholds = Thresholds(auto=0.4, alt=0.3, alt_margin=0.2, auto_min_evidence=2)
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.35, IN(CLAIMED)), (0.45, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.35, IN(CLAIMED)), (0.45, IN(ALT))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.35)
    assert sheet.s_alt == pytest.approx(0.45)
    assert sheet.s_alt - sheet.s_claimed < thresholds.alt_margin
    decision = route(sheet, thresholds, FLAGS_ON)
    assert isinstance(decision.action, Replace)


def test_route_below_min_evidence_low_score_quarantine_with_proposed_action():
    outcomes = [outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.applicable_count == 1 < DEFAULT_THRESHOLDS.auto_min_evidence
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "quarantine"
    assert decision.auto is False
    assert isinstance(decision.action, Replace)


# ---------------------------------------------------------------------------
# exact boundary cases
# ---------------------------------------------------------------------------


def test_route_boundary_s_claimed_equals_quarantine_is_matched():
    outcomes = [outcome("p1", 1.0, pairs=[(DEFAULT_THRESHOLDS.quarantine, IN(CLAIMED))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(DEFAULT_THRESHOLDS.quarantine)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "matched"


def test_route_boundary_s_claimed_equals_auto_is_quarantine():
    outcomes = [outcome("p1", 1.0, pairs=[(DEFAULT_THRESHOLDS.auto, IN(CLAIMED))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(DEFAULT_THRESHOLDS.auto)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON)
    assert decision.outcome == "quarantine"


def test_route_approval_required_demotes_otherwise_auto_remap_to_quarantine():
    # Same fixture as test_route_low_credible_same_series_alt_both_flags_on_
    # remediate_remap, which asserts auto=True/remediate with these flags
    # minus approval_required. approval_required=True must short-circuit
    # auto to False regardless of auto_remap/auto_replace/evidence.
    outcomes = [
        outcome("p1", 1.0, pairs=[(0.1, IN(CLAIMED)), (0.9, IN(ALT))]),
        outcome("p2", 1.0, pairs=[(0.05, IN(CLAIMED)), (0.85, IN(ALT))]),
    ]
    sheet = aggregate(outcomes, CLAIMED)
    decision = route(sheet, DEFAULT_THRESHOLDS, FLAGS_ON_APPROVAL_REQUIRED)
    assert decision.outcome == "quarantine"
    assert decision.auto is False
    assert isinstance(decision.action, Remap)
    assert decision.action.target_episode_ids == ALT
    assert "approval_required" in decision.reason


def test_route_boundary_alt_margin_exact_is_credible():
    thresholds = Thresholds(auto=0.3, alt=0.4, alt_margin=0.2, auto_min_evidence=2)
    # s_claimed=0.2, s_alt=0.4: alt >= 0.4 threshold exactly, margin 0.4-0.2=0.2 exactly.
    outcomes = [outcome("p1", 1.0, pairs=[(0.2, IN(CLAIMED)), (0.4, IN(ALT))])]
    sheet = aggregate(outcomes, CLAIMED)
    assert sheet.s_claimed == pytest.approx(0.2)
    assert sheet.s_alt == pytest.approx(0.4)
    decision = route(sheet, thresholds, FLAGS_ON)
    # Credible (boundary satisfied) + in_series alt -> Remap, regardless of auto.
    assert isinstance(decision.action, Remap)
    assert decision.action.target_episode_ids == ALT
