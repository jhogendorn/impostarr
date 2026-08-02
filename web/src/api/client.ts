import type {
  ApproveResponse,
  BackfillResponse,
  DeleteTrashResponse,
  GetQueueOptions,
  JobDetail,
  JobStatus,
  LogsResponse,
  QueuePage,
  RestoreTrashResponse,
  StatusResponse,
  TransitionResponse,
  TrashPage,
  VerdictRequest,
  VerdictResponse,
} from './types'

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, body: unknown) {
    super(`API error ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

const API_BASE = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api/v1`

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  if (!response.ok) {
    let body: unknown = text
    try {
      body = text ? JSON.parse(text) : null
    } catch {
      // non-JSON error body (e.g. a proxy/plaintext 5xx) — surface the raw text
    }
    throw new ApiError(response.status, body)
  }
  const body = text ? JSON.parse(text) : null
  return body as T
}

export function getStatus(): Promise<StatusResponse> {
  return api<StatusResponse>('/status')
}

export function getQueue(status: JobStatus, opts: GetQueueOptions = {}): Promise<QueuePage> {
  const { page = 1, pageSize = 50, instance, sort, dir } = opts
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (instance) params.set('instance', instance)
  if (sort) params.set('sort', sort)
  if (dir) params.set('dir', dir)
  return api<QueuePage>(`/queues/${status}?${params}`)
}

export function getJob(id: number): Promise<JobDetail> {
  return api<JobDetail>(`/jobs/${id}`)
}

/** URL for an asset's raw bytes (frame thumbnails etc) — for use as an
 * `<img src>`, not fetched through the `api()` JSON helper. */
export function assetUrl(jobId: number, assetId: number): string {
  return `${API_BASE}/jobs/${jobId}/assets/${assetId}`
}

export function postVerdict(id: number, req: VerdictRequest): Promise<VerdictResponse> {
  return api<VerdictResponse>(`/jobs/${id}/verdict`, {
    method: 'POST',
    body: JSON.stringify(req),
  })
}

export function approveJob(id: number): Promise<ApproveResponse> {
  return api<ApproveResponse>(`/jobs/${id}/approve`, { method: 'POST' })
}

export function rejectJob(id: number): Promise<TransitionResponse> {
  return api<TransitionResponse>(`/jobs/${id}/reject`, { method: 'POST' })
}

export function parkJob(id: number): Promise<TransitionResponse> {
  return api<TransitionResponse>(`/jobs/${id}/park`, { method: 'POST' })
}

export function unparkJob(id: number): Promise<TransitionResponse> {
  return api<TransitionResponse>(`/jobs/${id}/unpark`, { method: 'POST' })
}

export function rerunJob(id: number): Promise<TransitionResponse> {
  return api<TransitionResponse>(`/jobs/${id}/rerun`, { method: 'POST' })
}

export function triggerBackfill(name: string, batchSize?: number): Promise<BackfillResponse> {
  return api<BackfillResponse>(`/instances/${encodeURIComponent(name)}/backfill`, {
    method: 'POST',
    body: JSON.stringify(batchSize === undefined ? {} : { batch_size: batchSize }),
  })
}

export function getLogs(level?: string, limit = 200): Promise<LogsResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (level) params.set('level', level)
  return api<LogsResponse>(`/logs?${params}`)
}

export function getTrash(page = 1, pageSize = 50): Promise<TrashPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  return api<TrashPage>(`/trash?${params}`)
}

export function deleteTrashItem(id: number): Promise<DeleteTrashResponse> {
  return api<DeleteTrashResponse>(`/trash/${id}`, { method: 'DELETE' })
}

export function restoreTrashItem(id: number): Promise<RestoreTrashResponse> {
  return api<RestoreTrashResponse>(`/trash/${id}/restore`, { method: 'POST' })
}
