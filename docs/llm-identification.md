# What LLMs can identify, measured

Four experiments run against `gpt-4o-mini` with a real transcript excerpt
from a production job (American Dad S05E17, "Every Which Way But Lose" —
football dialogue). Recorded because the results overturn the obvious design
and explain a season's worth of wrong verdicts.

## The experiments

**1. No episode list; ask for database ids.**

```json
{"title":"The Best Christmas Story Never Told",
 "tvdb_episode_id":123456,"tmdb_episode_id":789012,"imdb_episode_id":"tt1234567",
 "season":6,"episode":3,"confidence":0.9}
```

Every id is a shaped-like-an-id placeholder. The title is wrong too (a
Christmas episode for football dialogue), at 0.9 confidence.

**Never ask a model for database identifiers.** They are arbitrary integers;
recall is nil and fabrication is confident.

**2. Episode list with titles only, opaque refs.**

Wrong episode (S05E05), but confidence dropped to 0.7/0.5/0.4 with an
unprompted "could fit multiple episodes" note. The reasoning shows it
matching *themes* ("losing", "father figure") because titles alone give it
nothing concrete to compare against.

**3. Episode list with titles + synopses, opaque refs, confidence rubric.**

Correct episode at 0.9, alternates at 0.5 and 0.3. ~1,451 prompt tokens,
about **$0.0003 per call**.

**Identification must be matching, not recall.** The synopses are the
load-bearing ingredient — not padding to be optimised away. Cost is
negligible at this granularity even for a 400-episode series.

**4. Same as 3, plus a declared season/episode and numbering convention.**

```
conf=0.85  ref → S05E17 'Every Which Way But Lose'   ← CORRECT
           model says S05E03 (aired-order)            ← MISMATCH
```

The model **selected the right episode and then emitted the wrong number for
it.** This is the root cause of the production false-mismatches: the original
prompt asked only for season/episode numbers, so a model that had identified
the content correctly still returned a wrong number, and we recorded a
confident mismatch.

## Consequences for the design

- **Opaque refs are the authoritative answer channel.** The model picks from
  a list we supply and answers with our own token; we map it back locally.
  Numbering-convention ambiguity (production vs aired order, the whole reason
  shows like American Dad are hard) becomes structurally impossible.
- **A declared season/episode + convention is worth requesting anyway**, as a
  cross-check. Disagreement with the ref's real numbering is a measurable
  confusion signal: record it in evidence and penalise confidence; never let
  the number override the ref.
- **Confidence needs an explicit rubric with anchors.** Without one every
  answer is 0.9. With one, experiment 2 produced honest hedging and
  experiment 3 produced calibrated ranking.
- **Structured reasoning beats prose instruction.** Asking in prose for
  verbatim quotes produced paraphrase; it needs to be a schema field
  (`evidence_quotes`) so the human reviewing a verdict can see what actually
  matched.
- **Constrain output with the API, not with pleading.** `json_schema` +
  `strict` where supported; under `json_object` the model returned refs
  wrapped in brackets (`"[e688]"`).
- **Do not scope the candidate list to the claimed season.** It leaks the
  answer's neighbourhood and makes cross-season misplacement undetectable.

## Interpreting the American Dad trial

17 of 20 episodes landed in quarantine. Two independent defects produced
that, and neither was "the files are mislabelled":

1. `whisper-subs` reported `confidence 0.0` for the claimed episode when it
   could not *fetch* that episode's reference subtitles — presenting "I could
   not measure this" as "certainly not this episode". Jobs where the plugin
   abstained entirely came out clean at 0.90; jobs where it partially worked
   were dragged into quarantine. Fixed; see the evidence-integrity invariant
   in `CLAUDE.md`.
2. The LLM plugins' number-only answer channel corrupted correct
   identifications, as above.

The season should be re-run as a calibration benchmark once the prompt
redesign lands. A correctly-labelled season should return overwhelmingly
matched; anything still flagged is then worth a human's attention.
