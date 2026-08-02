import { useEffect, useRef, useState } from 'react'
import type { JobUpdateEvent, SseEvent, StatsEvent } from './types'

const API_BASE = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api/v1`

const INITIAL_BACKOFF_MS = 1000
const MAX_BACKOFF_MS = 30000

/** Subscribes to `/api/v1/events`, dispatching parsed `job_update` and
 * `stats` events to `onEvent`. Reconnects on error with exponential
 * backoff (1s, doubling, capped at 30s), reset to 1s on a successful
 * (re)connection. Tears the connection down on unmount. Returns whether
 * the connection is currently open, for a status-dot style indicator. */
export function useEvents(onEvent: (event: SseEvent) => void): boolean {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let source: EventSource | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let backoff = INITIAL_BACKOFF_MS
    let stopped = false

    function connect() {
      source = new EventSource(`${API_BASE}/events`)

      source.onopen = () => {
        backoff = INITIAL_BACKOFF_MS
        setConnected(true)
      }

      source.addEventListener('job_update', (event) => {
        const data = JSON.parse((event as MessageEvent).data) as JobUpdateEvent
        onEventRef.current({ kind: 'job_update', data })
      })

      source.addEventListener('stats', (event) => {
        const data = JSON.parse((event as MessageEvent).data) as StatsEvent
        onEventRef.current({ kind: 'stats', data })
      })

      source.onerror = () => {
        setConnected(false)
        source?.close()
        if (stopped) return
        reconnectTimer = setTimeout(() => {
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS)
          connect()
        }, backoff)
      }
    }

    connect()

    return () => {
      stopped = true
      setConnected(false)
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      source?.close()
    }
  }, [])

  return connected
}
