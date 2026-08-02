import type {
  ApproveResponse,
  BackfillResponse,
  JobDetail,
  JobStatus,
  QueuePage,
  StatusResponse,
  TransitionResponse,
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
  const body = text ? JSON.parse(text) : null
  if (!response.ok) {
    throw new ApiError(response.status, body)
  }
  return body as T
}

export function getStatus(): Promise<StatusResponse> {
  return api<StatusResponse>('/status')
}

export function getQueue(status: JobStatus, page = 1, pageSize = 50): Promise<QueuePage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  return api<QueuePage>(`/queues/${status}?${params}`)
}

export function getJob(id: number): Promise<JobDetail> {
  return api<JobDetail>(`/jobs/${id}`)
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
