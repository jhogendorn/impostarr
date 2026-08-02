import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEvents } from './sse'

type Listener = (event: { data: string }) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private listeners: Record<string, Listener[]> = {}

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: Listener) {
    ;(this.listeners[type] ??= []).push(listener)
  }

  close() {
    this.closed = true
  }

  emit(type: string, data: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener({ data: JSON.stringify(data) })
    }
  }
}

describe('useEvents', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('connects to /api/v1/events and dispatches job_update/stats to onEvent', () => {
    const onEvent = vi.fn()
    renderHook(() => useEvents(onEvent))

    expect(FakeEventSource.instances).toHaveLength(1)
    const source = FakeEventSource.instances[0]
    expect(source.url).toBe('/api/v1/events')

    act(() => {
      source.emit('job_update', { type: 'job_update', job_id: 1, status: 'matched' })
    })
    expect(onEvent).toHaveBeenCalledWith({
      kind: 'job_update',
      data: { type: 'job_update', job_id: 1, status: 'matched' },
    })

    act(() => {
      source.emit('stats', { pending: 2 })
    })
    expect(onEvent).toHaveBeenCalledWith({ kind: 'stats', data: { pending: 2 } })
  })

  it('reconnects with backoff on error, capped and reset by a fresh connection', () => {
    renderHook(() => useEvents(vi.fn()))
    expect(FakeEventSource.instances).toHaveLength(1)

    // First error: reconnect scheduled after 1s.
    act(() => {
      FakeEventSource.instances[0].onerror?.()
    })
    expect(FakeEventSource.instances[0].closed).toBe(true)
    act(() => {
      vi.advanceTimersByTime(999)
    })
    expect(FakeEventSource.instances).toHaveLength(1)
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(FakeEventSource.instances).toHaveLength(2)

    // Second error without a successful open in between: backoff doubles to 2s.
    act(() => {
      FakeEventSource.instances[1].onerror?.()
    })
    act(() => {
      vi.advanceTimersByTime(1999)
    })
    expect(FakeEventSource.instances).toHaveLength(2)
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(FakeEventSource.instances).toHaveLength(3)
  })

  it('closes the connection on unmount and cancels any pending reconnect', () => {
    const { unmount } = renderHook(() => useEvents(vi.fn()))
    const source = FakeEventSource.instances[0]
    expect(source.closed).toBe(false)

    // Schedule a reconnect, then unmount before it fires.
    act(() => {
      source.onerror?.()
    })
    unmount()
    expect(source.closed).toBe(true)

    // The pending reconnect must not create a new EventSource post-unmount.
    act(() => {
      vi.advanceTimersByTime(30_000)
    })
    expect(FakeEventSource.instances).toHaveLength(1)
  })
})
