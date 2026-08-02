import { useEffect, useRef, useState } from 'react'
import { getLogs } from '../api/client'
import type { LogRecord } from '../api/types'

const POLL_MS = 3000
const LEVELS = ['INFO', 'WARNING', 'ERROR'] as const
type Level = (typeof LEVELS)[number]

interface LogDrawerProps {
  open: boolean
}

/** Bottom drawer, toggled from the status header's "Logs" button: polls
 * `/api/v1/logs` every 3s while open, filtered by the selected level.
 * DRY-RUN lines are highlighted amber so suppressed-write audit lines
 * stand out from ordinary activity. */
function LogDrawer({ open }: LogDrawerProps) {
  const [level, setLevel] = useState<Level>('INFO')
  const [logs, setLogs] = useState<LogRecord[]>([])
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return undefined

    let cancelled = false
    const fetchLogs = () => {
      getLogs(level)
        .then((data) => {
          if (!cancelled) setLogs(data.items)
        })
        .catch((err: unknown) => console.error('logs fetch failed', err))
    }
    fetchLogs()
    const timer = setInterval(fetchLogs, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [open, level])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [logs])

  if (!open) return null

  return (
    <div className="fixed inset-x-0 bottom-0 z-40 flex h-[40vh] flex-col border-t border-slate-800 bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
        <span className="text-sm font-medium text-slate-200">Logs</span>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          Level
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value as Level)}
            className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-slate-200"
          >
            {LEVELS.map((lvl) => (
              <option key={lvl} value={lvl}>
                {lvl}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-2 font-mono text-xs">
        {logs.map((log, i) => (
          <div
            key={`${log.ts}-${i}`}
            className={log.message.startsWith('DRY-RUN') ? 'text-amber-400' : 'text-slate-300'}
          >
            {log.ts} [{log.level}] {log.logger} — {log.message}
          </div>
        ))}
      </div>
    </div>
  )
}

export default LogDrawer
