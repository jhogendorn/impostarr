/** TS mirrors of `impostarr.api.routes` response payloads.
 *
 * Keys are kept snake_case to match the API verbatim — no client-side
 * renaming. Source of truth: src/impostarr/api/routes.py. */

export const JOB_STATUSES = [
  'hold',
  'pending',
  'active',
  'matched',
  'quarantine',
  'inconclusive',
  'error',
  'remediated',
] as const

export type JobStatus = (typeof JOB_STATUSES)[number]

// -- /status ----------------------------------------------------------

export interface InstanceSummary {
  name: string
  url: string
  history_watermark: string | null
  backfill_cursor: string | null
  last_polled_at: string | null
  last_backfilled_at: string | null
}

export interface ActiveJob {
  job_id: number
  instance: string | null
  series_id: number | null
  sonarr_path: string | null
  claimed_by: string | null
  claimed_at: string | null
  elapsed_s: number | null
}

export interface StatusSummary {
  unprocessed: number
  processed: number
}

export interface SystemStats {
  cpu_percent: number
  mem_percent: number
}

export interface RefsubsQuota {
  used: number
  limit: number
}

export interface StatusResponse {
  instances: InstanceSummary[]
  queues: Record<JobStatus, number>
  summary: StatusSummary
  system: SystemStats
  approval_required: boolean
  active_jobs: ActiveJob[]
  workers: { pool_size: number }
  dry_run: boolean
  trash_count: number
  paused: boolean
  refsubs_quota: RefsubsQuota | null
}

export interface PauseResponse {
  paused: boolean
}

// -- /queues/{status} ---------------------------------------------------

export type QueueSortField = 'updated_at' | 'created_at' | 'confidence' | 'series' | 'instance'
export type SortDir = 'asc' | 'desc'

export interface QueueFileSummary {
  series_id: number
  sonarr_path: string
  episode_ids: number[]
}

export interface QueueVerdictSummary {
  s_claimed: number | null
  s_alt: number | null
  outcome: string
}

export interface JobSummary {
  job_id: number
  status: JobStatus
  instance: string | null
  file: QueueFileSummary
  verdict: QueueVerdictSummary | null
  created_at: string
  updated_at: string
}

export interface QueuePage {
  total: number
  page_size: number
  items: JobSummary[]
}

export interface GetQueueOptions {
  page?: number
  pageSize?: number
  instance?: string
  sort?: QueueSortField
  dir?: SortDir
}

// -- /jobs/{id} -----------------------------------------------------------

export interface JobDetailJob {
  id: number
  status: JobStatus
  attempts: number
  created_at: string
  updated_at: string
}

export interface JobDetailFile {
  series_id: number
  episode_ids: number[]
  episode_file_id: number
  sonarr_path: string
  local_path: string | null
  size: number | null
  content_hash: string | null
  quality: string | null
  languages: string[] | null
  history_id: number | null
  download_id: string | null
  source_title: string | null
  indexer: string | null
  guid: string | null
}

export interface PluginResult {
  name: string
  version: string
  status: 'ok' | 'abstain' | 'error'
  reason: string | null
  candidates: unknown
  normalized: unknown
}

export interface HumanIdent {
  season: number
  episodes: number[]
}

export interface DupeInfo {
  duplicate_of_file_id: number
  similarity: number
  sonarr_path: string | null
}

export interface JobDetailVerdict {
  s_claimed: number | null
  s_alt: number | null
  outcome: string
  proposed_action: Record<string, unknown> | null
  remediation_log: unknown
  source: 'auto' | 'human'
  human_ident: HumanIdent | null
  dupe_info: DupeInfo | null
}

export interface Asset {
  id: number
  type: 'probe' | 'audio' | 'subs' | 'frames' | 'transcript'
  path: string | null
  has_path: boolean
  tool_meta: unknown
  payload: unknown | null
}

/** Live-fetched claimed-series cross-database ids, `null` when the lookup
 * failed or no instance runtime is configured (see routes.py
 * `_series_external_ids`). */
export interface SeriesExternalIds {
  title: string | null
  tvdb_id: number | null
  imdb_id: string | null
  tmdb_id: number | null
}

export interface FrameHashSummary {
  algo: string
  version: number
  n_frames: number
}

export interface PhashCorpusSummary {
  confidence: number
  source: 'auto' | 'human'
}

/** One Sonarr episode id resolved to human-readable form — see routes.py
 * `_episode_labels`. Keyed by episode id (as a string, JSON object key)
 * on `JobDetail.episode_labels` for every id referenced anywhere in the
 * payload (P0.5: never render a bare episode id). */
export interface EpisodeLabel {
  id: number
  season: number
  episode: number
  title: string | null
}

/** A subtitle track's cue text (timestamps discarded — see
 * `impostarr.plugins.subtitles.parse_srt`), for the three-way text
 * comparison's reference-subs column. `label` is season/episode-ish
 * (e.g. "S01E01") when derivable, else a generic fallback. */
export interface ReferenceSubtitleTrack {
  label: string
  language: string | null
  cues: string[]
}

export interface JobDetail {
  job: JobDetailJob
  instance: string | null
  external_ids: SeriesExternalIds | null
  file: JobDetailFile
  plugin_results: PluginResult[]
  verdict: JobDetailVerdict | null
  assets: Asset[]
  episode_labels: Record<string, EpisodeLabel>
  reference_subtitles: ReferenceSubtitleTrack[]
  frame_hash_present: boolean
  frame_hash: FrameHashSummary | null
  phash_corpus: PhashCorpusSummary | null
}

// -- /jobs/{id}/verdict ---------------------------------------------------

export interface VerdictRequest {
  verdict: 'is_claimed' | 'is_other' | 'ignore'
  ident?: HumanIdent | null
}

export interface ProposedRemap {
  kind: 'remap'
  target_episode_ids: number[]
}

export interface VerdictResponse {
  job_status: JobStatus
  verdict_id: number
  proposed_remap: ProposedRemap | null
}

// -- transition endpoints (approve/reject/park/unpark/rerun) ---------------

export interface ApproveResponse {
  result: JobStatus
}

export interface TransitionResponse {
  result: JobStatus
}

// -- backfill ---------------------------------------------------------------

export interface BackfillRequest {
  batch_size?: number
}

export interface BackfillResponse {
  created: number
}

// -- SSE --------------------------------------------------------------------

export interface JobUpdateEvent {
  type: 'job_update'
  job_id: number
  status: JobStatus
}

export type StatsEvent = Record<JobStatus, number>

export type SseEvent = { kind: 'job_update'; data: JobUpdateEvent } | { kind: 'stats'; data: StatsEvent }

// -- /logs --------------------------------------------------------------

export interface LogRecord {
  ts: string
  level: string
  logger: string
  message: string
  exc: string | null
}

export interface LogsResponse {
  items: LogRecord[]
}

// -- /trash ---------------------------------------------------------------

export interface TrashItem {
  id: number
  instance: string
  original_path: string
  trash_path: string
  series_id: number
  episode_ids: number[]
  size: number
  trashed_at: string
  expires_at: string
  expires_in_s: number
}

export interface TrashPage {
  total: number
  page_size: number
  items: TrashItem[]
}

export interface DeleteTrashResponse {
  result: 'deleted'
}

export interface RestoreTrashResponse {
  result: 'restored'
  original_path: string
  note: string
}
