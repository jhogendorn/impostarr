import { useEffect, useState } from 'react'
import type { ActiveJob, SystemStats } from '../api/types'
import { formatElapsed, pathBasename } from '../lib/format'

const TICK_MS = 1000

interface ActiveStripProps {
  activeJobs: ActiveJob[]
  system: SystemStats | undefined
}

function elapsedSeconds(job: ActiveJob, now: number): number {
  if (job.claimed_at) {
    const claimedMs = new Date(job.claimed_at).getTime()
    if (!Number.isNaN(claimedMs)) return (now - claimedMs) / 1000
  }
  return job.elapsed_s ?? 0
}

function SystemMeter({ system }: { system: SystemStats | undefined }) {
  return (
    <div className="flex shrink-0 items-center gap-3 text-xs text-slate-400">
      <span>CPU {system ? `${Math.round(system.cpu_percent)}%` : '—'}</span>
      <span>MEM {system ? `${Math.round(system.mem_percent)}%` : '—'}</span>
    </div>
  )
}

/** tdarr-style strip above the tabs: one card per currently-processing job
 * (`status.active_jobs`), with a ticking elapsed time and an indeterminate
 * progress bar — no per-stage progress exists in the backend, so an
 * animated bar + elapsed is the honest signal that work is happening.
 * Always shows a compact right-aligned system meter, even when idle. */
function ActiveStrip({ activeJobs, system }: ActiveStripProps) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (activeJobs.length === 0) return undefined
    const timer = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(timer)
  }, [activeJobs.length])

  if (activeJobs.length === 0) {
    return (
      <div className="flex items-center justify-between gap-4 border-b border-slate-800 bg-slate-900/60 px-6 py-2">
        <span className="text-xs text-slate-600">idle — no jobs processing</span>
        <SystemMeter system={system} />
      </div>
    )
  }

  return (
    <div className="flex items-center gap-4 border-b border-slate-800 bg-slate-900/60 px-6 py-2">
      <div className="flex flex-1 flex-wrap gap-2 overflow-x-auto">
        {activeJobs.map((job) => (
          <div
            key={job.job_id}
            className="min-w-56 flex-1 rounded-lg border border-slate-800 bg-slate-800/50 px-3 py-2"
          >
            <div className="flex items-center justify-between gap-2 text-xs text-slate-300">
              <span className="truncate font-medium">
                {job.instance ?? '—'} · {job.sonarr_path ? pathBasename(job.sonarr_path) : '—'}
              </span>
              <span className="shrink-0 text-slate-500">{formatElapsed(elapsedSeconds(job, now))}</span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-slate-500">
              <span>{job.claimed_by ?? 'unclaimed'}</span>
            </div>
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-slate-700/70">
              <div className="animate-indeterminate h-full w-1/3 rounded-full bg-indigo-500" />
            </div>
          </div>
        ))}
      </div>
      <SystemMeter system={system} />
    </div>
  )
}

export default ActiveStrip
