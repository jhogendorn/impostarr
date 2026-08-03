import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getJob, getStatus, pauseWorkers, resumeWorkers } from './client'

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses a successful JSON response', async () => {
    const body = { instances: [], queues: {}, workers: { pool_size: 0 } }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getStatus()).resolves.toEqual(body)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/status', expect.objectContaining({}))
  })

  it('throws a typed ApiError with status and body on non-2xx', async () => {
    const errorBody = { detail: 'job not found' }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(errorBody), { status: 404 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const rejection = getJob(999)
    await expect(rejection).rejects.toBeInstanceOf(ApiError)
    try {
      await rejection
      expect.unreachable()
    } catch (err) {
      const apiError = err as ApiError
      expect(apiError.status).toBe(404)
      expect(apiError.body).toEqual(errorBody)
    }
  })

  it('falls back to the raw text body when a non-2xx response is not valid JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('<html>502 Bad Gateway</html>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const rejection = getStatus()
    await expect(rejection).rejects.toBeInstanceOf(ApiError)
    try {
      await rejection
      expect.unreachable()
    } catch (err) {
      const apiError = err as ApiError
      expect(apiError.status).toBe(502)
      expect(apiError.body).toBe('<html>502 Bad Gateway</html>')
    }
  })

  it('pauseWorkers POSTs to /pause', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ paused: true }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(pauseWorkers()).resolves.toEqual({ paused: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/pause',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('resumeWorkers POSTs to /resume', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ paused: false }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(resumeWorkers()).resolves.toEqual({ paused: false })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/resume',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
