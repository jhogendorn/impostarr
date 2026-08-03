# Roadmap

Longer-horizon direction. Not a commitment list — near-term work is tracked
per release.

## Shared identification database

The endgame for the verification model: a community database keyed on
content fingerprints, so most files can be identified without transcription
or LLM calls at all. Each record would bundle:

- perceptual frame-hash sequence (phash)
- content hash (sha/xxh family)
- show + episode metadata idents (tvdb / tmdb / imdb / anime DBs)
- scene release naming data (observed names for this content, which encodes
  the scene-vs-canonical numbering disagreements in the wild)
- a normalized processing-cost "estimation quantum" (time/resource data from
  contributors' runs, normalized against their recorded hardware context) —
  lets any instance predict verification cost/ETA for content it hasn't
  seen yet

Local corpus tables are deliberately shaped as the seed of this. Not
scheduled; recorded so design decisions keep the door open (fingerprint
schemas versioned, run-context captured with timing data, external-id maps
not collapsed to a single provider).

## LLM cost visibility

Providers do not expose remaining credit/balance via API, so Impostarr
self-meters spend (tokens × configured per-model prices). Optional
reconciliation against provider usage APIs (OpenAI organization Usage API;
Anthropic Usage & Cost API) is possible where the operator supplies
admin-scoped keys — these are separate credentials from inference keys and
never required.

## Plugin taxonomy build-out

- llm-compare: transcript vs fetched reference subtitle, judged by an LLM
  (robust where fuzzy ratios fail: ASR noise, dubtitle divergence).
- vision-ident: framegrabs → vision model (experimental; pairs with the
  phash corpus).
- burned-in-subtitle OCR: frame sampling + caption-region crop + OCR + the
  text-identification path; budget/schedule-gated.
- character-recognition: identify which characters appear in sampled frames
  against the episode's expected cast (present/absent cast is an
  identification signal — guest characters especially). A purpose-trained
  face/character embedding model with per-series reference galleries is
  likely far cheaper than pushing frames at a general VLM; viability
  unproven — pinned for later exploration.
- Extraction-stage pluggability ("inspection plugins") once multiple
  identifiers consume shared derived artifacts like OCR text.

## Language model

Per-file language profiles, preferred-track policies (e.g. `ja+en` over
`ja`), unwanted-track exclusion (audio description/commentary),
correct-episode-wrong-language flagging, alt-track fallback on low
confidence, dupe taxonomy with language sets (true dupe / language variant /
misplacement).

## Distribution: prebuilt everything

Decision (2026-08-03): users are never expected to build infrastructure —
"ready out of the box, no assembly." Plugins distribute as prebuilt wheels
(pip specs resolve to binaries), not source builds and not per-plugin
containers. Implies CI publishes wheels for anything that would otherwise
need compilation — notably a Vulkan-enabled pywhispercpp wheel so Intel/AMD
iGPU transcription acceleration is an install-time choice, not a
compile-your-own exercise. An OpenVINO transcriber backend is the other
candidate for Intel acceleration; both slot in via the existing
`impostarr.transcribers` entry-point group.

## Maybe pile

- Plugin-reliability feedback: operators rate plugin results; sustained
  unreliability lowers a plugin's effective weight over time. Overlaps the
  calibration loop (verdict history already feeds per-plugin calibration) —
  the additional idea is an explicit rating affordance in the UI.
- Composable scoring DAGs as first-order installables: nestable
  `algo: [plugins | algo: [...]]` structures (consensus groups inside
  tiers inside fallbacks — at a certain point it's a DAG). Users install a
  named DAG (default, anime, community-published) rather than hand-wiring
  plugin lists; power users author their own. Explicitly NOT a node-flow
  editor UI — config-file/document shaped.
- Character-recognition identifier (see plugin taxonomy above).
