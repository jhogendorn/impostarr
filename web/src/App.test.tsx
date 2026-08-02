import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { SseEvent } from './api/types'
import { queuePageFixture, statusFixture } from './components/testFixtures'

const { getStatusMock, getQueueMock } = vi.hoisted(() => ({
  getStatusMock: vi.fn(),
  getQueueMock: vi.fn(),
}))

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>()
  return { ...actual, getStatus: getStatusMock, getQueue: getQueueMock }
})

let capturedHandler: ((event: SseEvent) => void) | null = null
vi.mock('./api/sse', () => ({
  useEvents: (handler: (event: SseEvent) => void) => {
    capturedHandler = handler
    return true
  },
}))

describe('App SSE wiring', () => {
  beforeEach(() => {
    getStatusMock.mockResolvedValue(statusFixture)
    getQueueMock.mockResolvedValue(queuePageFixture)
    capturedHandler = null
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('debounces a job_update SSE event into a single queue+status refetch 500ms later', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<App />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(getQueueMock).toHaveBeenCalledTimes(1)
    expect(getStatusMock).toHaveBeenCalledTimes(1)

    expect(capturedHandler).not.toBeNull()
    act(() => {
      capturedHandler?.({ kind: 'job_update', data: { type: 'job_update', job_id: 1, status: 'matched' } })
    })

    // not yet — refetch is debounced
    expect(getQueueMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })

    expect(getQueueMock).toHaveBeenCalledTimes(2)
    expect(getStatusMock).toHaveBeenCalledTimes(2)
  })

  it('debounced refetch targets the tab active when the timer fires, not when it was scheduled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<App />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    getQueueMock.mockClear()

    // schedule the debounced refetch while still on the default 'hold' tab
    act(() => {
      capturedHandler?.({ kind: 'job_update', data: { type: 'job_update', job_id: 1, status: 'matched' } })
    })

    // switch tabs mid-debounce-window
    act(() => {
      fireEvent.click(screen.getByRole('tab', { name: /^Quarantine/ }))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })

    const lastCall = getQueueMock.mock.calls.at(-1)
    expect(lastCall?.[0]).toBe('quarantine')
  })
})
