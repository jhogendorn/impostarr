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
}

export interface StatusResponse {
  instances: InstanceSummary[]
  queues: Record<JobStatus, number>
  workers: { pool_size: number }
  dry_run: boolean
}

// -- /queues/{status} ---------------------------------------------------

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
  file: QueueFileSummary
  verdict: QueueVerdictSummary | null
  created_at: string
  updated_at: string
}

export interface QueuePage {
  total: number
  items: JobSummary[]
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

export interface JobDetailVerdict {
  s_claimed: number | null
  s_alt: number | null
  outcome: string
  proposed_action: Record<string, unknown> | null
  remediation_log: unknown
  source: 'auto' | 'human'
  human_ident: HumanIdent | null
}

export interface Asset {
  id: number
  type: 'probe' | 'audio' | 'subs' | 'frames' | 'transcript'
  path: string | null
  has_path: boolean
  tool_meta: unknown
  payload: unknown | null
}

export interface JobDetail {
  job: JobDetailJob
  file: JobDetailFile
  plugin_results: PluginResult[]
  verdict: JobDetailVerdict | null
  assets: Asset[]
  frame_hash_present: boolean
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
}

export interface LogsResponse {
  items: LogRecord[]
}
