# Impostarr — PoC Design

**Date:** 2026-08-02
**Status:** Approved draft (pre-implementation)

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
auto-learning, non-Sonarr libraries, UI-generated API keys.

## Topology

Single Python service (FastAPI) containing web/API/UI serving and an internal
worker pool. Workers consume a `jobs` table using claim/lease semantics
(atomic status flip with worker id + heartbeat). That table is the deliberate
seam for future Tdarr-style remote worker nodes: a job is a serializable spec
in, serializable result out, no shared in-process state. No network transport
for workers is built in the PoC.

GPU is used opportunistically: asset extraction uses faster-whisper CUDA when
available. Deploy target is "one box that may have a GPU"; a `-cuda` image
variant is provided.

## Pipeline

Per configured Sonarr instance:

1. **Discovery:** poll Sonarr history API for new import events on a
   configurable interval (Decluttarr/Cleanuparr convention). A manual,
   rate-limited **backfill** mode walks the existing library through the same
   pipeline. Optional per-instance path filters ("watch dirs") restrict which
   files are eligible.
2. **Queues:** discovered items enter `hold` → `pending` → `active`.
3. **Active processing, stage 1 — asset extraction** (cached per file content
   hash; reused across re-runs): audio slice → whisper transcript; embedded
   subtitle extraction / OCR text; sampled frames → perceptual hash +
   framegrab thumbnails; ffprobe metadata. Artifacts stored under `/assets`,
   paths recorded in DB.
4. **Active processing, stage 2 — identifier plugins** run over cached assets.
5. **Aggregation & routing** (below) → terminal queues: `matched`,
   `quarantine`, `inconclusive`, plus remediation history.

## Plugin contract

A plugin receives `(claimed_ident, assets, series_context)` and returns an
array of candidates:

```
[{ confidence: 0.0–1.0,
   ident: { series, season, episode } | null,
   numbering: "tvdb" | "tmdb" | "absolute" | "scene",
   evidence: { ...plugin-specific } }]
```

- Yes/no plugins return a single element scored against the claimed ident.
- Identifying plugins (subtitle matchers, future phash lookup) return N ranked
  candidates — e.g. low confidence on the labelled ident, high on a different
  one.
- Core normalizes every candidate's numbering to the Sonarr/TVDB ident using
  Sonarr's own episode list (which carries `absoluteEpisodeNumber` for anime)
  before aggregation. Sonarr series objects expose tvdbId/imdbId/tmdbId for
  cross-DB mapping.

### PoC plugins (approach 2 → 3)

Two plugins built by importing the existing tools as libraries (versions
pinned), each isolated behind the contract so a later native rewrite
(approach 3) is invisible to the rest of the system:

- `whisper-subs` — mkv-episode-matcher internals: whisper transcript vs
  reference subtitles per candidate episode.
- `subs-llm` — tvidentify internals: embedded subs (SRT or OCR'd PGS/VobSub)
  + LLM episode identification.

### Plugin loading

Python entry-point group `impostarr.identifiers`. Built-ins ship in-package.
External plugin collections are declared in config as pip specs (including
`git+https://…#subdirectory=…`) and installed into the app venv at container
boot, then discovered via entry points. This gives Go-style "list of repos"
UX while pip handles ML dependency resolution.

## Scoring & routing

Weighted aggregation per normalized candidate across plugins (weights in
config). Two derived values:

- `S_claimed` — aggregate confidence the file IS the labelled episode.
- `S_alt` — confidence of the best alternate candidate.

Routing (thresholds user-tunable; examples only):

| Condition | Outcome |
|---|---|
| `S_claimed ≥ quarantine_threshold` (e.g. 0.8) | `matched` |
| `auto_threshold ≤ S_claimed < quarantine_threshold` | `quarantine` (human review) |
| `S_claimed < auto_threshold` (e.g. 0.4) | automatic remediation, verb by `S_alt` |
| No extractable assets (no transcript, no subs, no usable frames) | `inconclusive` — never auto-acted |

Remediation verb selection when `S_claimed < auto_threshold`:

- **Strong same-series alternate** (`S_alt` high, same series): **remap** —
  Sonarr manual-import the file to the correct episode, where it
  quality-competes under Sonarr's own upgrade logic or fills an empty slot.
  Never blocklists; this prevents refetch/blacklist loops caused by
  scene-vs-TVDB numbering disagreements, and preserves high-quality
  mislabelled releases.
- **No credible alternate / different series / junk:** **replace** — delete
  file + blocklist release + trigger re-search (Decluttarr verb set).
- `auto_remap` and `auto_replace` are independently toggleable; when disabled,
  the item queues for human approval of the proposed action instead.

Rationale for the inconclusive carve-out: absence of evidence (no reference
subs, foreign audio, no embedded subs) is not evidence of imposture;
auto-blacklisting a legitimate file that merely lacks reference material is
the worst possible failure mode.

Human verdicts from `quarantine`/`inconclusive` — "is X", "isn't X",
"ignore" — are stored as gold records (`source: human`).

## Perceptual-hash flywheel

- Every processed file gets a phash computed during asset extraction (cheap
  relative to whisper).
- A `(phash, tvdb_id, tmdb_id, anidb_id, season, episode, confidence,
  source)` record is stored **only when verdict confidence ≥
  `phash_store_threshold`**; human verdicts are highest-signal.
- Local corpus use in PoC: duplicate detection under different names.
- Long-term aim (out of PoC scope): a public phash DB mapping phash →
  {tvdb, tmdb, anidb} idents with confidence — StashDB model for mainstream
  TV. The schema above is deliberately shaped as its seed. A future
  `phash-corpus` identifier plugin becomes just another entry in the plugin
  set with zero pipeline changes.

## Data model

SQLite default (`/config/impostarr.db`, WAL mode); Postgres supported via DSN
in config. SQLAlchemy + Alembic; portable types only (JSON not JSONB, no pg
arrays).

Tables: `instances`, `jobs` (status machine + lease fields), `files` (path,
size, content hash), `assets` (per-file cached extraction artifacts; paths
under `/assets`), `plugin_results` (raw candidate arrays), `verdicts` (final
routing + human overrides, `source: auto|human`), `phashes` (as above). All
media processing results are cached in the DB — re-verification reuses
assets and prior plugin results where inputs are unchanged.

## Configuration

Single YAML file, bind-mounted at `/config/impostarr.yml`. Every key
overridable via env vars: `IMPOSTARR__SECTION__KEY` (IaC-friendly; compose/k8s
first-class). No UI config surface in the PoC.

Contents:

- `sonarr[]`: url, api key, container↔sonarr path mappings, watch-dir path
  filters, poll interval, per-instance enable flags for auto verbs.
- `thresholds`: quarantine, auto, phash_store.
- `plugins`: external pip sources, per-plugin enable + weight.
- `auth`: trusted header name (e.g. `X-Authentik-Username`), optional group
  header/value; `api_keys: [{name, key}]`.
- `db`: optional Postgres DSN (absent → SQLite).
- `workers`: pool size, whisper model/device.

## Auth

Identity layer only; authz is future work. Requests with no credential run as
`anon` with admin rights — securing the stack is the deployer's job
(Authentik or similar in front). A present trusted header attributes identity;
static config-declared API keys (`X-Api-Key`) give automation an attributable
identity. Nothing is required to access the app.

## UI

Vite + React + TypeScript + Tailwind + Headless UI (echoes
Jellyseerr/Cleanuparr look), built to static files served by the FastAPI
container. Read-and-act only (no config editing):

- Status header: instances, worker activity, throughput.
- Queue tabs: hold / pending / active / matched / quarantine / inconclusive /
  history. Live updates via SSE.
- **Inspect modal** per item: Sonarr claimed mapping + cross-DB ids,
  per-plugin candidate arrays with evidence, transcript excerpt, framegrabs,
  ffprobe summary; human-verdict and remediation-approval actions.

## Deployment

- Docker image: CPU default, `-cuda` variant. Compose example provided;
  k8s-friendly by construction (file+env config, discrete volumes, `/healthz`).
- Volumes:
  - `/config` — config, state, SQLite DB. Backup-relevant.
  - `/assets` — extracted artifacts (transcripts, framegrabs). Can grow large;
    separately mountable (longhorn/NFS). Backup-relevant state.
  - `/models` — whisper/LLM model caches. Multi-GB; separately mountable;
    disposable.
  - `/media` — library mount; read-only unless in-place remap renames are
    enabled.
- Postgres, when used, runs as its own container/service.

## Open questions / future work

- Threshold defaults need empirical tuning against a real library.
- Numbering-offset learning (detect systematic scene↔TVDB offsets per series
  and suggest/apply mapping) — future; PoC only avoids the failure mode by
  never blocklisting same-series mislabels.
- Remote worker transport (claim API over HTTP) when single-node throughput
  is insufficient.
- Public phash DB service + community contribution flow.
- Radarr support (movie identification has no episode-level reference-subtitle
  trick; likely phash + transcript-vs-movie-subs).
