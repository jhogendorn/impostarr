# Impostarr — PoC Design

**Date:** 2026-08-02
**Status:** CONVERGED (buddysystem codex loop, 4 rounds, 2026-08-02) — ready for implementation planning

## Purpose

Post-import content verification for Sonarr libraries. Impostarr proves the bytes
on disk are the episode Sonarr thinks they are, scores confidence via pluggable
identifier plugins, and remediates (remap / replace / quarantine) through
Sonarr's API. It fills a gap in the *arr ecosystem: Sonarr trusts release
names; existing checkers (checkrr etc.) verify only that files decode, not that
they are what they claim.

Ecosystem position: same lifecycle slot as Decluttarr/Cleanuparr (API-driven,
polls Sonarr, remediates via Sonarr verbs), not Tdarr (no filesystem watching,
no transcoding).

## Scope

**In (PoC):** Sonarr (multiple instances), post-import verification, backfill
scans, two identifier plugins (wrapping mkv-episode-matcher and tvidentify
internals), weighted confidence scoring, queue UI, remap/replace/quarantine
remediation, human verdicts, local phash accumulation, SQLite default +
Postgres option, docker compose / k8s deployment.

**Out (explicitly):** Radarr, remote worker nodes, import gating (pre-import
verification), public phash service, UI-managed config, numbering-offset
auto-learning, non-Sonarr libraries, UI-generated API keys, physical
quarantine (quarantine is a review queue only — no file moves).

## Topology

Single Python service (FastAPI) containing web/API/UI serving and an internal
worker pool. Workers consume a `jobs` table using claim/lease semantics
(atomic status flip with worker id + heartbeat). That table is the deliberate
seam for future Tdarr-style remote worker nodes: a job is a serializable spec
in, serializable result out, no shared in-process state. No network transport
for workers is built in the PoC.

GPU is used opportunistically: asset extraction uses faster-whisper with
`device: auto|cpu|cuda` (default `auto`, falling back to CPU). CUDA operation
requires the `-cuda` image variant, which pins a CUDA runtime + cuDNN
combination compatible with the pinned CTranslate2/faster-whisper versions.
Deploy target is "one box that may have a GPU".

## Pipeline

Per configured Sonarr instance:

1. **Discovery:** poll Sonarr history API for new import events on a
   configurable interval (Decluttarr/Cleanuparr convention). Discovery is
   idempotent and resumable: a per-instance history watermark (last processed
   history record id) is persisted, and items are deduplicated by
   `(instance, episode_file_id)` and file content hash. A manual, rate-limited
   **backfill** mode walks the existing library (episode files API) through
   the same pipeline with its own persisted cursor. Optional per-instance
   path filters ("watch dirs") restrict which files are eligible. At
   discovery time, Sonarr metadata needed for later remediation is captured
   (see data model): series/episode/episodeFile ids, grab history where it
   exists (`downloadId`, `sourceTitle`, indexer, guid), quality, languages.
2. **Queues:** discovered items enter `pending`. `hold` is a parked state:
   items land there when the user pauses them, or when backfill rate-limiting
   defers them; parked items move to `pending` on user release or when the
   rate limiter grants a slot.
3. **Active processing, stage 1 — asset extraction** (cached per file content
   hash; reused across re-runs): audio slice → whisper transcript; embedded
   subtitle extraction / OCR text; sampled frames → perceptual frame-hash
   sequence + framegrab thumbnails; ffprobe metadata. Artifact blobs are
   stored under `/assets`; the DB stores metadata, hashes, and paths (DB =
   metadata and fingerprints, `/assets` = blobs; losing `/assets` loses
   evidence/thumbnails but not verdict history).
4. **Active processing, stage 2 — identifier plugins** run over cached assets.
5. **Aggregation & routing** (below) → terminal queues: `matched`,
   `quarantine`, `inconclusive`, plus remediation history.

### Reference subtitle service

Reference subtitles are a shared service used by identifier plugins, not a
per-plugin concern. It is modeled explicitly: provider (OpenSubtitles API)
credentials in config, a per-episode cache under `/assets/refsubs/`, rate and
daily-quota awareness (OpenSubtitles enforces daily download limits), and a
manual local-SRT drop-in directory that takes precedence. When reference
subtitles are unavailable for an episode, dependent plugins **abstain** (they
do not emit low confidence).

## Plugin contract

A plugin receives `(claimed_ident, assets, series_context)` where
`series_context` includes the Sonarr series (with external ids and full
episode list including scene numbering fields) and the reference subtitle
service. It returns:

```
{ status: "ok" | "abstain" | "error",
  reason: string | null,          # required for abstain/error
  candidates: [                    # present when status == ok
    { confidence: 0.0–1.0,
      ident: { series: "claimed" | { tvdb?: int, tmdb?: int, imdb?: str },
               season: int, episodes: [int, ...] } | null,
      numbering: "tvdb" | "tmdb" | "absolute" | "scene" | null,
      evidence: { ...plugin-specific } } ] }
```

- `episodes` is an array to support multi-episode files (S01E01E02) and
  maps to one or more Sonarr episode ids after normalization. Specials are
  season 0 under tvdb numbering. `series` is `"claimed"` for in-series
  candidates (the common case); a plugin that identifies content as a
  *different* series supplies that series' external ids instead.
- `ident: null` expresses negative/junk evidence — "this content is not the
  claimed episode and no ident could be established" — with `confidence` as
  the strength of that negative claim. `numbering` is null when `ident` is
  null (and only then).
- **Every `ok` result MUST include a candidate for the claimed ident** (its
  assessed confidence, even if 0.0). Identifying plugins additionally return
  ranked alternates.
- `abstain` means the plugin could not apply (no embedded subs, no reference
  subs, unsupported language); `error` means it tried and failed (LLM/quota/
  crash). Neither contributes to scoring; both are recorded.
- Yes/no plugins return the single claimed-ident candidate.
- Core normalizes every candidate's numbering to Sonarr episode ids using
  Sonarr's episode list: `absoluteEpisodeNumber` for absolute,
  `sceneSeasonNumber`/`sceneEpisodeNumber`/`sceneAbsoluteEpisodeNumber`
  (TheXEM-sourced) for scene numbering where present, and series-level
  tvdb/tmdb/imdb ids for cross-DB mapping. Candidates that cannot be
  normalized are recorded but excluded from routing.

### PoC plugins (approach 2 → 3)

Two plugins built by importing the existing tools as libraries (versions
pinned), each isolated behind an adapter module so a later native rewrite
(approach 3) is invisible to the rest of the system. Both are CLI-oriented
tools, so their internals are an integration risk — the adapter owns their
config, cache layout, and any fork/vendoring decision:

- `whisper-subs` — mkv-episode-matcher internals: whisper transcript vs
  reference subtitles per candidate episode. Requires: TMDB API key,
  reference subtitle service. Confidence derives from match ratio over
  compared lines.
- `subs-llm` — tvidentify internals: embedded subs (SRT or OCR'd PGS/VobSub)
  + LLM episode identification. Requires: an LLM provider (OpenAI-compatible
  endpoint or Ollama; provider, base URL, model, API key in plugin config)
  and OCR/system deps (`tesseract`, `mkvtoolnix`, `ffmpeg`/`ffprobe`) which
  ship in the image.

### Plugin loading

Python entry-point group `impostarr.identifiers`. Built-ins ship in-package.
External plugin collections are declared in config as **pinned** pip specs
(including `git+https://…@tag#subdirectory=…`) and installed at container
boot into a dedicated venv overlay persisted under `/config/plugins/venv`,
then discovered via entry points. Boot-time install is a deliberate
UX/reproducibility tradeoff: install is skipped when the pinned spec set is
unchanged (lock hash), failures disable the plugin rather than the app, and
production deployments are advised to bake a custom image instead.

## Scoring & routing

All scoring operates on normalized candidates (Sonarr episode-id sets).
Plugins with status `abstain`/`error` are excluded; let `A` be the applicable
plugin set with configured weights `w_i` and reported confidences `c_i`.

- `S(candidate) = Σ_{i∈A reporting candidate} w_i·c_i / Σ_{i∈A reporting candidate} w_i`
  (weighted mean over plugins that reported the candidate).
- `S_claimed = S(claimed ident)` — defined whenever `A` is non-empty, because
  every applicable plugin must score the claimed ident.
- `S_alt` = max `S(c)` over non-claimed candidates reported by at least one
  identifying plugin. An alternate is **credible** when
  `S_alt ≥ thresholds.alt` AND `S_alt − S_claimed ≥ thresholds.alt_margin`
  (both configurable). Plugin authors are responsible for calibrating raw signal to
  0–1; per-plugin weight is the operator's tuning knob.

Routing (thresholds user-tunable; examples only):

| Condition | Outcome |
|---|---|
| `A` empty (all plugins abstained/errored — no evidence either way) | `inconclusive` — never auto-acted |
| `S_claimed ≥ thresholds.quarantine` (e.g. 0.8) | `matched` |
| `thresholds.auto ≤ S_claimed < thresholds.quarantine` | `quarantine` (human review) |
| `S_claimed < thresholds.auto` (e.g. 0.4) | remediation, verb by credible-alternate rule |

Auto-remediation additionally requires `|A| ≥ thresholds.auto_min_evidence` (default 2 —
both PoC plugins); below that, the item routes to `quarantine` with the
proposed action attached instead of acting. This separates negative evidence
(plugins looked and disagreed with the label) from thin evidence.

Remediation verb selection when `S_claimed < thresholds.auto`:

- **Credible same-series alternate** → **remap** (choreography below). Never
  blocklists; this prevents refetch/blacklist loops caused by scene-vs-TVDB
  numbering disagreements and preserves high-quality mislabelled releases.
- **No credible alternate, or the credible candidate is a different series
  (`ident.series` ≠ claimed) or junk (`ident: null`)** →
  **replace**: delete file via Sonarr episodeFile delete + blocklist (only
  when a grab-history record exists — see below) + trigger episode search.
- `auto_remap` and `auto_replace` are independently toggleable; when disabled,
  the item queues for human approval of the proposed action instead.

Rationale for the inconclusive carve-out: absence of evidence (no reference
subs, foreign audio, no embedded subs) is not evidence of imposture;
auto-blacklisting a legitimate file that merely lacks reference material is
the worst possible failure mode.

Human verdicts from `quarantine`/`inconclusive` — "is X", "isn't X",
"ignore" — are stored as gold records (`source: human`).

### Remediation mechanics

**Replace.** Blocklisting in Sonarr is history-record based (mark grab as
failed). It is only possible when discovery captured a grab-history record
for the file. Choreography: if grab history exists → mark failed (blocklists
+ triggers Sonarr's failed handling); always → delete the episodeFile via
API (removes file from disk) and trigger an `EpisodeSearch` command.
Backfilled/manually-imported files without grab history get the
no-blocklist fallback: delete + search only, flagged in history as
"unblocklistable".

**Remap.** Sonarr has no atomic "reassign this episodeFile" operation; remap
is a manual-import choreography using a per-instance **staging directory**
(configured; must be Sonarr-visible and on the same filesystem as the
library so hardlinks work):

1. Hardlink (fallback copy) the file into staging under a name matching the
   corrected ident.
2. Delete the original episodeFile record via API (this removes the original
   path; the hardlinked staging copy preserves the data).
3. Invoke manual import (`ManualImport` command) mapping the staging file to
   the correct episode ids with `importMode: move`.
4. On failure at step 3, the staging file remains on disk; the job is routed
   to `quarantine` with the error and the staging path for operator recovery.
   Steps are recorded per-job for auditability.

**Occupied-slot policy.** Manual import does not arbitrate quality. If the
remap target episode already has a file: auto-remap does not fire; the item
routes to `quarantine` with a proposed remap and both files' quality info
(from Sonarr) shown for human decision. Auto-remap fires only into empty
slots in the PoC. (Future: `auto_remap_upgrade` comparing quality-profile
ranks.)

## Perceptual-hash flywheel

- Every processed file gets a **frame-hash sequence** computed during asset
  extraction (cheap relative to whisper): N frames sampled at deterministic
  timestamps → per-frame perceptual hash, stored with algorithm name +
  version and sample schedule. Sequence similarity (per-frame hamming
  distance + alignment threshold) is the comparison primitive; a single
  whole-file hash is too weak at episode scale.
- A corpus record `(frame_hash_seq_ref, external_ids, season, episodes,
  confidence, source)` is stored **only when the verdict carries a positive
  normalized ident with confidence ≥ `thresholds.phash_store`** — a match, a
  remap target, or a human "is X". Negative verdicts ("isn't X",
  replace-with-no-alternate) remain gold in `verdicts` but create no corpus
  record. Human verdicts are highest-signal; a human positive verdict has
  defined confidence 1.0 (and therefore always clears the threshold).
  `external_ids` is a JSON map (`{tvdb, tmdb, imdb, mal, anilist, ...}`)
  populated from what Sonarr exposes for the series — Sonarr does not expose
  AniDB ids, so AniDB mapping is a future provider, not a stored claim.
- Local corpus use in PoC: duplicate detection under different names.
- Long-term aim (out of PoC scope): a public phash DB mapping frame-hash
  sequences → external-id idents with confidence — StashDB model for
  mainstream TV. The schema above is deliberately shaped as its seed. A
  future `phash-corpus` identifier plugin becomes just another entry in the
  plugin set with zero pipeline changes.

## Data model

SQLite default (`/config/impostarr.db`, WAL mode); Postgres supported via DSN
in config. SQLAlchemy + Alembic; portable types only (JSON not JSONB, no pg
arrays). SQLite implies single-replica deployment; Postgres is required for
any future multi-replica or remote-worker operation.

Tables (key fields, not exhaustive):

- `instances` — sonarr url/config ref, history watermark, backfill cursor.
- `jobs` — status machine + lease fields (worker id, heartbeat), timestamps.
- `files` — instance_id, sonarr path, local path, size, content hash, and
  captured Sonarr identifiers: series_id, episode_ids (JSON), episode_file_id,
  quality, languages, and grab-history metadata where present (history id,
  download_id, source_title, indexer, guid).
- `assets` — per-file cached extraction artifacts: type, `/assets` path,
  input fingerprint, tool versions.
- `plugin_results` — plugin name/version, status, raw candidate array,
  normalized candidates, and an **input fingerprint** (hash of plugin
  version, plugin config, model identifiers, asset hashes, reference-sub
  cache state, claimed ident, and the relevant series context — external ids
  + episode list including scene numbering) — results are reused only on
  fingerprint match.
- `verdicts` — S_claimed, S_alt, routing outcome, remediation steps +
  results, human overrides, `source: auto|human`.
- `frame_hashes` / `phash_corpus` — as described in the flywheel section.

All media processing metadata is cached in the DB; re-verification reuses
assets and prior plugin results when fingerprints are unchanged.

## Configuration

Single YAML file, bind-mounted at `/config/impostarr.yml`. Env overrides:
`IMPOSTARR__SECTION__KEY` for scalar keys; list- or object-valued keys are
overridden by supplying the value as JSON (e.g.
`IMPOSTARR__SONARR='[{"url": …}]'`). Indexed list syntax is not supported.
No UI config surface in the PoC.

Contents:

- `sonarr[]`: url, api key, container↔sonarr path mappings, staging dir,
  watch-dir path filters, poll interval, per-instance enable flags for auto
  verbs.
- `thresholds`: `quarantine`, `auto`, `alt`, `alt_margin`,
  `auto_min_evidence`, `phash_store` — the canonical key names used
  throughout this spec as `thresholds.<key>`.
- `plugins`: external pip specs (pinned), per-plugin enable + weight +
  plugin-specific config (TMDB key; LLM provider/base-url/model/key).
- `refsubs`: OpenSubtitles credentials, quota budget, cache dir, manual SRT
  directory.
- `auth`: trusted header name (e.g. `X-Authentik-Username`), optional group
  header/value; `api_keys: [{name, key}]`.
- `db`: optional Postgres DSN (absent → SQLite).
- `workers`: pool size, whisper model/device.

## Auth

Identity layer only; authz is future work and auth is effectively
**disabled by default**: every request runs with admin rights. A present
trusted header (Authentik forward-auth) attributes identity for audit; when
a group header/value is configured it is enforced as a gate (403 otherwise).
Static config-declared API keys (`X-Api-Key`) give automation an
attributable identity. Securing the stack is the deployer's job.

## UI

Vite + React + TypeScript + Tailwind + Headless UI (echoes
Jellyseerr/Cleanuparr look), built to static files served by the FastAPI
container. Read-and-act only (no config editing):

- Status header: instances, worker activity, throughput.
- Queue tabs: hold / pending / active / matched / quarantine / inconclusive /
  history. Live updates via SSE.
- **Inspect modal** per item: Sonarr claimed mapping + external ids,
  per-plugin candidate arrays with evidence and abstain/error reasons,
  transcript excerpt, framegrabs, ffprobe summary; human-verdict and
  remediation-approval actions (including occupied-slot remap decisions).

## Deployment

- Docker image: CPU default; `-cuda` variant with pinned CUDA/cuDNN matching
  the pinned CTranslate2. System deps in image: ffmpeg/ffprobe, mkvtoolnix,
  tesseract. Compose example provided; k8s-friendly by construction
  (file+env config, discrete volumes, `/healthz`). SQLite default implies
  single replica; use Postgres otherwise.
- Volumes:
  - `/config` — config, state, SQLite DB, plugin venv overlay.
    Backup-relevant.
  - `/assets` — extracted artifact blobs (transcripts, framegrabs, refsub
    cache). Can grow large; separately mountable (longhorn/NFS).
    Backup-relevant state.
  - `/models` — whisper/LLM model caches. Multi-GB; separately mountable;
    disposable.
  - `/media` — library mount. Requires write access only for remap staging
    (hardlink + manual import); read-only otherwise.
- Postgres, when used, runs as its own container/service.

## Open questions / future work

- Threshold defaults need empirical tuning against a real library.
- Numbering-offset learning (detect systematic scene↔TVDB offsets per series
  and suggest/apply mapping) — future; PoC only avoids the failure mode by
  never blocklisting same-series mislabels.
- Remote worker transport (claim API over HTTP) when single-node throughput
  is insufficient.
- Public phash DB service + community contribution flow; AniDB id mapping
  provider.
- `auto_remap_upgrade`: automatic occupied-slot remap when the mislabelled
  file outranks the incumbent in the series' quality profile.
- Radarr support (movie identification has no episode-level reference-subtitle
  trick; likely phash + transcript-vs-movie-subs).
