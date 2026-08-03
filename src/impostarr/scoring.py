"""Scoring and routing: the pure-logic core of Impostarr's identification.

Pure functions, no I/O. Mirrors the spec's "Scoring & routing" section
exactly.

`aggregate()` consumes plugin results that have *already* been normalized
(by `impostarr.normalize.normalize`, called by the pipeline against a
`SeriesContext`) — this module never needs series context itself, only the
normalized candidate keys and the claimed episode-id set.

Claimed-key convention: every `ok` `PluginResult` is contractually guaranteed
(by `PluginResult`'s validator) to include a candidate with
`ident.series == "claimed"`. If that candidate — or any other of the
plugin's in-series candidates — normalizes to exactly the claimed episode-id
set, its confidence contributes naturally to the claimed key. If none does
(the plugin looked at the claimed episode and concluded something else, or
its claimed-series candidate was unnormalizable), the plugin is treated as
reporting 0.0 confidence on the claimed key — it still counts in the
claimed key's weighted average, at 0.0, while its actual (differing)
candidate scores normally under its own key and may become the alternate.

Within-plugin dedupe: if a single plugin reports two candidates that
normalize to the *same* key (e.g. two differently-worded candidates that
both resolve to the same episode-id set), that plugin contributes only its
max confidence for that key — never double-counted, never averaged with
itself.
"""

from __future__ import annotations

import math
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from .config import ScoringConfig, Thresholds
from .normalize import (
    CrossSeriesCandidate,
    InSeriesCandidate,
    JunkCandidate,
    NormalizedCandidate,
    Unnormalizable,
)
from .plugins.base import PluginResult

# frozenset[int] for in_series; ("cross", sorted external-id items) for
# cross_series; ("junk",) for junk.
CandidateKey = Any


class PluginOutcome(NamedTuple):
    plugin_name: str
    weight: float
    result: PluginResult
    normalized: list[NormalizedCandidate | Unnormalizable]


class ScoreSheet(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    s_claimed: float | None
    s_alt: float | None
    alt_key: CandidateKey | None = None
    alt_kind: Literal["in_series", "cross_series", "junk"] | None = None
    applicable_count: int
    per_candidate: dict[str, float] = Field(default_factory=dict)
    # Plugins whose report on a given candidate was excluded as an outlier
    # (see ScoringConfig.outlier_rejection) -- one dict per exclusion, in
    # {"plugin": str, "confidence": float, "candidate": str (key-repr)}
    # shape, so the evidence/UI can surface why a low report didn't drag
    # that candidate's score down.
    outliers_excluded: list[dict[str, Any]] = Field(default_factory=list)


class Remap(BaseModel):
    kind: Literal["remap"] = "remap"
    target_episode_ids: frozenset[int]


class Replace(BaseModel):
    kind: Literal["replace"] = "replace"


class InstanceFlags(BaseModel):
    auto_remap: bool
    auto_replace: bool
    # When true, no auto decision is ever returned (see route()) — every
    # remediation candidate demotes to quarantine with a proposed action,
    # regardless of auto_remap/auto_replace. Populated from the top-level
    # Settings.approval_required flag by the pipeline.
    approval_required: bool = False


class RoutingDecision(BaseModel):
    outcome: Literal["matched", "quarantine", "inconclusive", "remediate"]
    action: Remap | Replace | None
    auto: bool
    reason: str


def _key_for(norm: NormalizedCandidate) -> CandidateKey:
    if isinstance(norm, InSeriesCandidate):
        return norm.episode_ids
    if isinstance(norm, CrossSeriesCandidate):
        return ("cross", tuple(sorted(norm.external_ids.items())))
    if isinstance(norm, JunkCandidate):
        return ("junk",)
    raise TypeError(f"not a NormalizedCandidate: {norm!r}")


def _kind_of_key(key: CandidateKey) -> Literal["in_series", "cross_series", "junk"]:
    if isinstance(key, frozenset):
        return "in_series"
    if key[0] == "cross":
        return "cross_series"
    if key[0] == "junk":
        return "junk"
    raise TypeError(f"unrecognized candidate key shape: {key!r}")


def _key_repr(key: CandidateKey) -> str:
    if isinstance(key, frozenset):
        return "in_series:" + ",".join(str(i) for i in sorted(key))
    if key[0] == "cross":
        items = ",".join(f"{k}={v}" for k, v in key[1])
        return f"cross:{items}"
    return "junk"


# A single plugin's report on one candidate key: (plugin_name, weight, confidence).
_Report = tuple[str, float, float]

# `aggregate()`'s own default when no `ScoringConfig` is passed (all existing
# callers, incl. every test in test_scoring.py) -- linear fusion, no outlier
# rejection, i.e. byte-for-byte the pre-fusion-strategy behavior. Production
# (pipeline.py) always passes `deps.settings.scoring` explicitly, whose own
# default (`ScoringConfig()`) is "logodds" -- see config.py.
_LEGACY_DEFAULT_CONFIG = ScoringConfig(fusion="linear", outlier_rejection=False)


def _fuse(reports: list[_Report], cfg: ScoringConfig) -> float:
    sum_w = sum(weight for _, weight, _ in reports)
    if sum_w <= 0:
        return 0.0
    if cfg.fusion == "linear":
        return sum(weight * confidence for _, weight, confidence in reports) / sum_w
    # logodds: clamp to [eps, 1-eps] (logit(0)/logit(1) are +-inf), weighted
    # mean in log-odds space, mapped back through the logistic function.
    # Decisive (near 0/1) reports dominate hedging (near 0.5) ones instead of
    # every plugin pulling the mean toward itself with equal leverage.
    #
    # A single report has no pooling to do at all -- return it (clamped)
    # directly rather than round-tripping through log/exp, which is not
    # exactly invertible in floating point (logistic(logit(0.9)) evaluates
    # to 0.8999999999999999, not 0.9) and would otherwise make an
    # unpooled score spuriously fail an exact-equality/boundary threshold
    # check it should pass.
    if len(reports) == 1:
        return min(max(reports[0][2], cfg.eps), 1.0 - cfg.eps)
    total_logit = 0.0
    for _name, weight, confidence in reports:
        c = min(max(confidence, cfg.eps), 1.0 - cfg.eps)
        total_logit += weight * math.log(c / (1.0 - c))
    mean_logit = total_logit / sum_w
    return 1.0 / (1.0 + math.exp(-mean_logit))


def _reject_outliers(
    key: CandidateKey, reports: list[_Report], cfg: ScoringConfig, outliers_excluded: list[dict[str, Any]]
) -> list[_Report]:
    """Per `ScoringConfig.outlier_rejection`: if >= `outlier_min_agreeing`
    reports on this candidate are >= `outlier_high` and every *other* report
    is <= `outlier_low`, exclude those low reports from the pool entirely and
    append them to `outliers_excluded`. Returns `reports` unchanged
    otherwise (including when rejection is disabled)."""
    if not cfg.outlier_rejection:
        return reports
    high = [r for r in reports if r[2] >= cfg.outlier_high]
    low = [r for r in reports if r[2] < cfg.outlier_high]
    if len(high) < cfg.outlier_min_agreeing or not low:
        return reports
    if not all(r[2] <= cfg.outlier_low for r in low):
        return reports
    for name, _weight, confidence in low:
        outliers_excluded.append(
            {"plugin": name, "confidence": confidence, "candidate": _key_repr(key)}
        )
    return high


def aggregate(
    results: list[PluginOutcome],
    claimed_episode_ids: frozenset[int],
    scoring: ScoringConfig | None = None,
) -> ScoreSheet:
    cfg = scoring if scoring is not None else _LEGACY_DEFAULT_CONFIG
    claimed_key: CandidateKey = frozenset(claimed_episode_ids)

    applicable = [outcome for outcome in results if outcome.result.status == "ok"]
    applicable_count = len(applicable)
    if applicable_count == 0:
        return ScoreSheet(
            s_claimed=None,
            s_alt=None,
            alt_key=None,
            alt_kind=None,
            applicable_count=0,
            per_candidate={},
            outliers_excluded=[],
        )

    # key -> [(plugin_name, weight, confidence), ...]
    reports_by_key: dict[CandidateKey, list[_Report]] = {}

    for name, weight, result, normalized in applicable:
        per_plugin: dict[CandidateKey, float] = {}
        for cand, norm in zip(result.candidates, normalized, strict=True):
            if isinstance(norm, Unnormalizable):
                continue
            key = _key_for(norm)
            per_plugin[key] = max(per_plugin.get(key, cand.confidence), cand.confidence)

        # Contract: this plugin must have submitted a claimed-series
        # candidate. If none of its normalized candidates landed exactly on
        # the claimed key, it's treated as a 0.0 report on that key.
        per_plugin.setdefault(claimed_key, 0.0)

        for key, confidence in per_plugin.items():
            reports_by_key.setdefault(key, []).append((name, weight, confidence))

    outliers_excluded: list[dict[str, Any]] = []
    scores: dict[CandidateKey, float] = {}
    for key, reports in reports_by_key.items():
        pool = _reject_outliers(key, reports, cfg, outliers_excluded)
        scores[key] = _fuse(pool, cfg)

    # claimed_key is always present: every applicable plugin's per_plugin dict
    # gets a claimed_key entry (natural or 0.0-injected) above, and
    # applicable_count > 0 was already checked, so accum/scores is non-empty
    # and includes claimed_key.
    s_claimed = scores[claimed_key]

    non_claimed = {key: score for key, score in scores.items() if key != claimed_key}
    if non_claimed:
        # Tie-break: max() returns the first maximal element it encounters;
        # iterating in ascending key-repr order makes ties resolve to the
        # lexicographically-smallest key-repr, deterministically.
        alt_key = max(sorted(non_claimed, key=_key_repr), key=lambda k: non_claimed[k])
        s_alt = non_claimed[alt_key]
        alt_kind = _kind_of_key(alt_key)
    else:
        alt_key = None
        s_alt = None
        alt_kind = None

    per_candidate = {_key_repr(key): score for key, score in scores.items()}

    return ScoreSheet(
        s_claimed=s_claimed,
        s_alt=s_alt,
        alt_key=alt_key,
        alt_kind=alt_kind,
        applicable_count=applicable_count,
        per_candidate=per_candidate,
        outliers_excluded=outliers_excluded,
    )


def route(sheet: ScoreSheet, thresholds: Thresholds, flags: InstanceFlags) -> RoutingDecision:
    if sheet.s_claimed is None:
        return RoutingDecision(
            outcome="inconclusive", action=None, auto=False, reason="no applicable evidence"
        )

    s_claimed = sheet.s_claimed

    if s_claimed >= thresholds.quarantine:
        return RoutingDecision(
            outcome="matched",
            action=None,
            auto=False,
            reason=f"s_claimed={s_claimed:.3f} >= quarantine threshold {thresholds.quarantine}",
        )

    if s_claimed >= thresholds.auto:
        return RoutingDecision(
            outcome="quarantine",
            action=None,
            auto=False,
            reason=(
                f"s_claimed={s_claimed:.3f} in [{thresholds.auto}, {thresholds.quarantine}) "
                "- human review"
            ),
        )

    # s_claimed < thresholds.auto: remediation path.
    credible = (
        sheet.s_alt is not None
        and sheet.s_alt >= thresholds.alt
        and (sheet.s_alt - s_claimed) >= thresholds.alt_margin
    )

    if credible and sheet.alt_kind == "in_series":
        action: Remap | Replace = Remap(target_episode_ids=sheet.alt_key)
        action_kind = "remap"
    else:
        action = Replace()
        action_kind = "replace"

    flag = flags.auto_remap if action_kind == "remap" else flags.auto_replace
    auto = (
        sheet.applicable_count >= thresholds.auto_min_evidence
        and flag
        and not flags.approval_required
    )
    outcome = "remediate" if auto else "quarantine"

    s_alt_str = f"{sheet.s_alt:.3f}" if sheet.s_alt is not None else "None"
    reason = (
        f"s_claimed={s_claimed:.3f} < auto threshold {thresholds.auto}; "
        f"{'credible' if credible else 'no credible'} alternate "
        f"(s_alt={s_alt_str}); proposing {action_kind}"
    )
    if not auto:
        if flags.approval_required:
            reason += "; not auto (approval_required mode: awaiting human approval)"
        else:
            reason += (
                f"; not auto (applicable_count={sheet.applicable_count} vs "
                f"auto_min_evidence={thresholds.auto_min_evidence}, {action_kind} flag={flag})"
            )

    return RoutingDecision(outcome=outcome, action=action, auto=auto, reason=reason)
