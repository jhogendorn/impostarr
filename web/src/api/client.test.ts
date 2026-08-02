import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getJob, getStatus } from './client'

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
})
