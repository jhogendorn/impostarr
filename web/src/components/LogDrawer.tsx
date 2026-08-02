import { useEffect, useRef, useState } from 'react'
import { getLogs } from '../api/client'
import type { LogRecord } from '../api/types'
import { formatClock } from '../lib/format'

const POLL_MS = 3000
const LEVELS = ['INFO', 'WARNING', 'ERROR'] as const
type Level = (typeof LEVELS)[number]

interface LogDrawerProps {
  open: boolean
}

const LEVEL_CHIP_CLASS: Record<Level, string> = {
  INFO: 'bg-slate-700 text-slate-300',
  WARNING: 'bg-amber-500/20 text-amber-400',
  ERROR: 'bg-red-500/20 text-red-400',
}

function levelChipClass(level: string): string {
  return LEVEL_CHIP_CLASS[level as Level] ?? 'bg-slate-700 text-slate-300'
}

/** Bottom drawer, toggled from the status header's "Logs" button: polls
 * `/api/v1/logs` every 3s while open, filtered by the selected level (chips,
 * not a select — same three levels, click to switch). Each line renders
 * structured: level chip, hh:mm:ss time, dimmed logger name, message.
 * DRY-RUN lines are highlighted amber so suppressed-write audit lines stand
 * out from ordinary activity. */
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
        <div className="flex items-center gap-2">
          {LEVELS.map((lvl) => (
            <button
              key={lvl}
              type="button"
              aria-pressed={level === lvl}
              onClick={() => setLevel(lvl)}
              className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                level === lvl ? levelChipClass(lvl) : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>
      {/* min-h-0 is load-bearing: a flex child's default min-height is
       * "auto", which lets it grow to fit its content and overrides the
       * fixed-height parent instead of scrolling within it — flex-1 alone
       * does not fix this (classic flexbox-overflow gotcha). */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-2 font-mono text-xs">
        {logs.map((log, i) => (
          <div
            key={`${log.ts}-${i}`}
            className={`flex items-baseline gap-2 py-0.5 ${
              log.message.startsWith('DRY-RUN') ? 'text-amber-400' : 'text-slate-300'
            }`}
          >
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold ${levelChipClass(log.level)}`}>
              {log.level}
            </span>
            <span className="shrink-0 text-slate-500">{formatClock(log.ts)}</span>
            <span className="shrink-0 text-slate-600">{log.logger}</span>
            <span>{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default LogDrawer
