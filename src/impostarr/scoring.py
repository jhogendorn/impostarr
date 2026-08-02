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

from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from .config import Thresholds
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


class Remap(BaseModel):
    kind: Literal["remap"] = "remap"
    target_episode_ids: frozenset[int]


class Replace(BaseModel):
    kind: Literal["replace"] = "replace"


class InstanceFlags(BaseModel):
    auto_remap: bool
    auto_replace: bool


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


def aggregate(results: list[PluginOutcome], claimed_episode_ids: frozenset[int]) -> ScoreSheet:
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
        )

    # key -> [sum(weight * confidence), sum(weight)]
    accum: dict[CandidateKey, list[float]] = {}

    for _name, weight, result, normalized in applicable:
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
            acc = accum.setdefault(key, [0.0, 0.0])
            acc[0] += weight * confidence
            acc[1] += weight

    scores: dict[CandidateKey, float] = {
        key: (sum_wc / sum_w if sum_w > 0 else 0.0) for key, (sum_wc, sum_w) in accum.items()
    }

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
    auto = sheet.applicable_count >= thresholds.auto_min_evidence and flag
    outcome = "remediate" if auto else "quarantine"

    s_alt_str = f"{sheet.s_alt:.3f}" if sheet.s_alt is not None else "None"
    reason = (
        f"s_claimed={s_claimed:.3f} < auto threshold {thresholds.auto}; "
        f"{'credible' if credible else 'no credible'} alternate "
        f"(s_alt={s_alt_str}); proposing {action_kind}"
    )
    if not auto:
        reason += (
            f"; not auto (applicable_count={sheet.applicable_count} vs "
            f"auto_min_evidence={thresholds.auto_min_evidence}, {action_kind} flag={flag})"
        )

    return RoutingDecision(outcome=outcome, action=action, auto=auto, reason=reason)
