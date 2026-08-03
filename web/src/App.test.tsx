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

  it('wraps the tab bar and table together in one inset glow-panel card (not separate full-bleed rings)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<App />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    const tabList = screen.getByRole('tablist')
    // TabGroup (HeadlessUI) renders its own unstyled wrapper div around
    // TabList, so the card is the tablist's grandparent, not its parent.
    const card = tabList.parentElement!.parentElement!
    expect(card).toHaveClass('glow-panel', 'rounded-xl')
    // the table lives inside the same card as the tab bar
    expect(card).toContainElement(screen.getByRole('table'))
    // neither child re-applies its own ring now that the card owns it
    expect(tabList).not.toHaveClass('glow-panel')
  })

  it('the initial queue fetch requests the same page size the Records-per-page select displays', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<App />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(getQueueMock).toHaveBeenCalledTimes(1)
    const [, opts] = getQueueMock.mock.calls[0]
    const requestedPageSize = (opts as { pageSize: number }).pageSize

    expect(screen.getByLabelText('Records per page')).toHaveValue(String(requestedPageSize))
  })
})
