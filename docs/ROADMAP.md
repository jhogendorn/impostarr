# Roadmap

Direction we might take: ideas whose shape is not settled, recorded so design
decisions keep their options open. Nothing here is committed and anything
here may be superseded.

Once an item's shape is agreed — enough that someone could start it without
needing another decision — it moves to `docs/BACKLOG.md` and is deleted from
here. The test is "could a fresh contributor start this tomorrow without
asking a question only the owner can answer?".

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


## Plugin taxonomy build-out

(Settled language, transcription and prompt work lives in `docs/BACKLOG.md`;
what remains here is genuinely unsettled.)

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
