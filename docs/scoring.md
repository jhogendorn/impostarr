# Scoring: fusion strategies and outlier rejection

`scoring.aggregate()` turns each identifier plugin's per-candidate
confidence into one score per candidate (`ScoreSheet.s_claimed`/`s_alt`),
which `scoring.route()` then routes against `Thresholds`. How those
per-plugin confidences get pooled into a single number is controlled by
`ScoringConfig` (`settings.scoring` in `impostarr.yml`).

## The problem: linear pooling and confident agreement

The original (and still available) pooling is a weighted arithmetic mean:
`sum(weight * confidence) / sum(weight)`. It treats every plugin's opinion
as equally informative regardless of how decisive it is. That's fine when
plugins mostly agree, but it lets one hedging or wrong plugin drag down two
independently confident ones — because a mean has no concept of
"decisiveness."

Production case (job 14, American Dad S05E07): two independent plugins
(`subs-llm`, `transcript-llm`) each reported 0.90 confidence on the claimed
episode. A third (`whisper-subs`) reported 0.00 on it — not because the
episode was wrong, but because it had matched against a reference subtitle
for a *different* episode (a numbering disagreement, confirmed in this
show's data). Linear pooling: `(0.90 + 0.90 + 0.00) / 3 = 0.600` — squarely
in the quarantine mid-band, discarding two confident agreements because of
one mis-sourced input.

## `fusion: "logodds"` (default)

Instead of averaging confidences directly, each confidence is converted to
log-odds (`logit(p) = ln(p / (1-p))`), the log-odds are combined via a
weighted mean, and the result is mapped back through the logistic function
(`sigmoid`). Log-odds space is where Bayesian-style evidence combination
naturally lives: `logit(0.9) ≈ 2.20` and `logit(0.99) ≈ 4.60` — the last
9% carries almost twice the "evidence weight" of the first 90%, because
`logit` is unbounded and steep near 0 and 1. A near-0.5 (hedging) report
contributes almost nothing (`logit(0.5) = 0`); confident reports dominate.

Confidences are clamped to `[eps, 1-eps]` before the transform (`eps`,
default `0.02`) since `logit(0)`/`logit(1)` are `-inf`/`+inf`.

Job 14 under logodds *alone* (no outlier rejection): `logit(0.9) = 2.197`
(×2) and `logit(0.02) = -3.892` (whisper-subs' 0.00 clamped to eps) →
mean log-odds `≈ 0.167` → `sigmoid(0.167) ≈ 0.542`. Better than 0.600's
naive read might suggest relative to how decisive the two 0.9s are, but
still short of the 0.8 quarantine threshold — a single dissenting report,
even down-weighted by log-odds geometry, still has real pull when it's
one-in-three votes. Fusion strategy alone isn't enough; see below.

`fusion: "linear"` is kept, byte-for-byte, as the original weighted mean —
for back-compat and as the strategy to diff against when comparing
behavior.

## Outlier rejection

Independently of the fusion strategy, `outlier_rejection` (default `true`)
detects a specific pattern per candidate: if at least `outlier_min_agreeing`
(default `2`) plugins report that candidate at `>= outlier_high` (default
`0.8`) **and every other reporting plugin** is at `<= outlier_low` (default
`0.2`), the low reporters are excluded from that candidate's pool entirely
(not down-weighted — removed), and the exclusion is recorded on the
`ScoreSheet` as `outliers_excluded`: a list of
`{"plugin": ..., "confidence": ..., "candidate": ...}` records, so the
evidence view can show *why* a low report didn't move the score.

Rationale: a plugin scoring ~0 on a candidate that two *independent*
plugins call a ~90%+ match is far more likely to be looking at the wrong
input entirely (e.g. it matched against a reference subtitle that is
actually for a different episode) than to be correctly catching a mismatch
the other two both missed. This is exactly the American Dad numbering-
disagreement failure mode: `whisper-subs`' reference subtitle was
mis-sourced, not the file.

The rule is evaluated per candidate key, not globally — a plugin can be
treated as an outlier on the claimed key while its report on some other
candidate (if any) is left untouched, since the "≥2 agree, rest are near
zero" pattern is specific to that one candidate's evidence.

Job 14 with logodds + outlier rejection (the default `ScoringConfig()`):
`whisper-subs`' 0.00 is excluded from the claimed key's pool. Only the two
0.90 reports remain → `s_claimed ≈ 0.9` → **matched**, clearing the 0.8
quarantine threshold.

| Mode                          | s_claimed | Outcome            |
|--------------------------------|-----------|---------------------|
| linear (today's default)       | 0.600     | quarantine (mid-band) |
| logodds alone                  | ≈0.542    | quarantine (mid-band) |
| logodds + outlier rejection    | ≈0.900    | **matched**            |

## Configuration

```yaml
scoring:
  fusion: logodds        # "linear" | "logodds"
  eps: 0.02               # confidence clamp for the logit transform (logodds only)
  outlier_rejection: true
  outlier_high: 0.8        # >= this, per agreeing plugin, to count toward outlier_min_agreeing
  outlier_low: 0.2         # <= this, for every non-agreeing plugin, to be excluded
  outlier_min_agreeing: 2  # how many plugins must agree at outlier_high to trigger rejection
```

All fields are optional; the defaults above (logodds + outlier rejection on)
are also `ScoringConfig()`'s own Python-level defaults.

## Calibration caveat

Both of these are heuristics layered on top of confidences that are
themselves **uncalibrated** — the LLM-based plugins (`subs-llm`,
`transcript-llm`) report a "confidence" that has no guarantee of matching
observed accuracy (a model saying "90% confident" is not necessarily right
90% of the time). Log-odds pooling and outlier rejection make the
aggregate more robust to *known* failure shapes (one bad input outvoting
two good ones) but do not fix the underlying calibration problem. Proper
calibration (e.g. reliability curves against verified outcomes, temperature
scaling, or isotonic regression per plugin) is tracked as future work — see
`docs/ROADMAP.md` — and should eventually replace `eps`/`outlier_*` as
hand-picked constants with values derived from measured plugin behavior.
