# Backlog

Agreed work, not yet started. Ordered roughly by dependency, not priority.
Each item records the decision already made so it doesn't get re-litigated.

## 1. LLM prompt + schema redesign

The current prompts are the biggest known source of wrong verdicts. Settled
by live experiment (see `docs/llm-identification.md`):

- Include the episode list **with synopses** — this is load-bearing, not
  padding. Identification must be *matching*, not *recall*.
- Answers come back as **opaque refs** (`[e688]`) mapped locally to episode
  ids. Never ask for database ids (hallucinated) and never trust a returned
  season/episode number as the answer.
- Also request `season`/`episode`/`numbering_convention` as a **cross-check**:
  ref-vs-number disagreement is a confusion signal worth recording in
  evidence and penalising, never overriding the ref.
- Drop the season scoping (currently leaks the claimed season and makes
  cross-season misplacement undetectable).
- Ranked multi-candidate output, empty list and "none of these" allowed.
- `response_format: json_schema` with `strict` where supported (refs came
  back as `"[e688]"` with brackets under json_object).
- Confidence rubric with explicit anchors; generic/formulaic dialogue must
  score low across all candidates rather than forcing a pick.
- `evidence_quotes: [string]` as a structured field — prose instruction to
  quote produced paraphrase instead. Feeds the inspect panel's reasoning
  tooltip so a human can audit what actually matched.
- Provenance: record the model from the **response body** (not inferred from
  config, which is wrong under failover) plus an `attempts` list of providers
  tried and why they fell through.

## 2. Plugin instances, provider groups, tiered execution

- Named **instances** of a plugin type in config (e.g. `subs-llm-gpt4o`,
  `subs-llm-qwen`), each with its own config and weight, so several models
  can run side by side (local via Ollama/LocalAI + cloud).
- Per-group behaviour: `failover` (shipped), `consensus` (intra-group vote
  before entering scoring as one signal), `individual` (each an independent
  signal — interacts with correlation discounting in item 3).
- **Tiered execution with early exit**: order plugins by cost tier (free and
  local first, paid APIs last); after each tier, if the result is already
  decisive, skip the rest. New plugin-result status `skipped` (distinct from
  `abstain`); min-evidence accounting must understand skips.
  "If we know the answer early, stop verifying."

## 3. Evidence-fusion research spike

Should confident plugins carry more weight? Survey and backtest: linear vs
logarithmic opinion pools, per-plugin calibration (Platt/isotonic) from
accumulated verdict history + human gold labels, Dempster–Shafer (models
abstention natively, matching our contract), IR rank fusion, and correlation
discounting for same-family plugins (the two LLM plugins are not independent
witnesses; nor are two refsub-based ones).

Deliverable: a chosen formula, calibration data requirements, migration path
from the current pooling, and a **backtest harness over stored verdicts**.
Run this only once there is post-fix verdict volume — backtesting against
verdicts produced by the old broken engine is worthless.

Concrete evidence to test against: job 14 (American Dad S05E07) scored
`s_claimed` 0.600 under linear pooling with two plugins at 0.90 and one
outlier at 0.00 — the case that motivated the current heuristics.

## 4. Language matrix

- Per-file **language profile** (ffprobe stream tags + whisper detection, all
  tracks recorded in evidence).
- Per-instance **preferred-track policy** (anime commonly wants `ja+en` over
  `ja`); **unwanted-track exclusion** (audio description, commentary, via
  disposition/title); alt-track fallback when all plugins return low
  confidence.
- Capture Sonarr's `originalLanguage` per series.
- New verdict dimension: **correct episode, wrong language** — a flagged
  annotation, not a plain match.
- **Dupe taxonomy** with audio-language sets: true dupe (same languages),
  language variant (legitimate, not remediable), misplacement (same content
  under a different series). Requires language sets on phash corpus rows.
- Dubtitle caveat: subtitles for a dub are usually translated from the
  original script, not transcribed from the dub audio, so same-language
  comparisons on dubs score structurally lower. Record the audio-language /
  refsub-language pairing in evidence so a depressed score is explainable.
- Demo harness gains a language scenario matrix (dubbed, dual-audio,
  non-English embedded subs).

## 5. Telemetry: LLM spend + plugin timing

- Meter spend ourselves: token usage per call × a configurable per-model
  price table, cumulative per provider in `/status` and the header, optional
  budget. **No provider exposes remaining credit via API** — self-metering is
  the baseline; optional reconciliation against provider usage APIs (OpenAI
  organization Usage API, Anthropic Usage & Cost API) needs admin-scoped keys
  that are separate credentials from inference keys and never required.
- `plugin_results` rows gain cost fields (tokens in/out, cost, provider,
  model) and timing (`duration_s`) plus a run-context snapshot (cpu count,
  memory, gpu presence, pool size, transcriber backend + compute type).
  Extraction stages get the same. Reruns already append superseding rows, so
  cost/time per show or per period becomes a query.
- Aggregates endpoint (per provider, per series, per day).
- Feeds ETA estimation for the Active strip, and eventually the shared-DB
  "estimation quantum" in `docs/ROADMAP.md`.

## 6. Auto-action grace delay

Config `auto_grace_delay`: when auto remediation would fire, the job instead
enters quarantine with the proposal and an `apply_at` timestamp; the worker
applies it when the delay elapses unless a human intervenes.

- `apply_at` is already plumbed through the API and the inspect panel renders
  a countdown slot when present (currently always null).
- The **Now** button is always present on the countdown — forcing the
  currently-scheduled action immediately is always allowed.
- Clicking any action button while a countdown is pending **replaces** the
  scheduled action: the countdown re-arms and the Proposed Action section
  updates live (label, icon, tint).
- `Dismiss` cancels the pending auto-apply entirely.
- `approval_required: true` disables auto and the countdown wholesale.

## 7. Transcription quality, cross-matching, refsub sourcing

- **Whisper prompt priming**: pass an `initial_prompt` corpus of show terms
  (character names, locations, jargon) — a large accuracy win on jargon-heavy
  shows. Cheap sources: series/episode overviews (already fetched). Richer:
  TMDB cast, wiki synthesis. Cache per series.
- **Text-panel cross-matching**: clicking a line in one panel fuzzy-matches
  (Levenshtein/LCS/token-set) into the other two and highlights + scrolls to
  the matches. Investigate efficiency: precomputed alignment at extraction
  time vs on-click search; DTW-style sequence alignment; timestamp-anchored
  windowing (now viable since transcripts carry absolute times).
- **Refsub cache independence**: cache keyed only by (external id, season,
  episode, language), independent of job/DB rows, so deleting job data never
  re-spends quota. Add **negative-result caching** (searched, found nothing)
  with a TTL.
- **More subtitle providers.** Primary investigation: use **Bazarr as a
  meta-provider** — it already fans out to ~40 providers with credentials,
  quotas and anti-captcha handled, and manages the same Sonarr library.
  Determine whether its API can fetch subtitles for an arbitrary episode
  without attaching them to a file; if so it likely replaces per-provider
  integration wholesale. Otherwise integrate tvsubtitles.net, addic7ed.com,
  subdl.com alongside OpenSubtitles.
  Also evaluate Bazarr's existing **sidecar `.srt` files on disk** — zero API
  cost, but note the independence caveat: a sidecar was fetched for *that
  file* based on its claimed identity, so it is weak evidence for confirming
  the same episode and strong evidence only for cross-episode comparison.

## 8. UI: icon actions and bulk column design

One icon per action, used both as icon-only buttons in the queue table and
alongside text labels in the inspect panel's action bar, colour-matched:

| Action | Icon | Tone |
| --- | --- | --- |
| Reidentify | circular arrow | slate |
| Mark Correct | tick | green |
| Trash and Regrab | trash can | red |
| Dismiss | X | slate |
| Ignore Mismatch | zzz / sleep | slate |
| Apply Remap | double caret right | indigo |
| Now (force auto-apply) | lightning bolt | indigo |

- Apply Remap in the table needs room for a small searchbar plus the caret;
  if the searchbar gets its own "Content Identity" column, the apply button
  stays with it.
- The auto-apply countdown + Now button get their own column (needs item 6).
- Bulk bar: same icons; **apply stands alone** and applies each selected
  row's *own* top match across the selection, not one shared target.
- Icon-only buttons require `aria-label`, a visible tooltip on hover/focus,
  and a focus ring. Non-negotiable — icon-only controls are exactly where
  discoverability regressions hide.
- No icon library is installed yet (lucide-react or heroicons).

## 9. Deployment follow-ups (29c.sh)

Tracked in the infrastructure repo, recorded here for context:

- Config-checksum pod annotation so config/Secret changes roll the deployment
  (env from a Secret does not live-reload; a manual `rollout restart` was
  needed).
- Revisit `pool_size` (currently 1) and the 6Gi memory limit once the shared
  whisper-model lock and transcription semaphore are proven under load.
- OpenSubtitles VIP decision — the free tier's 20 downloads/day is roughly one
  season per day of reference-subtitle throughput.
- CI-built wheels (including a Vulkan-enabled `pywhispercpp`) per the
  "prebuilt everything" stance in `docs/ROADMAP.md`.

## 10. Live-trial probe sequence

One show at a time, each chosen to stress a specific failure class:

1. **No Heroics** (done) — clean baseline, 6/6 matched.
2. **American Dad S05** (done) — numbering disagreement; surfaced the
   whisper-subs false-zero bug and the LLM number-channel corruption.
   **Re-run this season as the calibration benchmark** after the LLM prompt
   redesign lands; a correctly-labelled season should come back
   overwhelmingly matched, and anything still flagged is a real finding.
3. **Mushoku Tensei** — dupes, misnumbered episodes, Japanese audio. Needs
   the `sonarr-anime` instance added to the kit and language-aware refsubs
   (item 4).
4. **Dubbed foreign** (e.g. Squid Game) — exercises language handling in the
   other direction.
5. Gradual `watch_dirs` widening, gated on throttles and the OpenSubtitles
   quota decision. Expect permission-skip counts in backfill responses to act
   as a drift detector.
