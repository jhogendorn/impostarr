import type { InstanceSummary, StatusResponse } from '../api/types'
import { relativeTime } from '../lib/format'

interface StatusHeaderProps {
  status: StatusResponse | null
  connected: boolean
  onToggleLogs: () => void
}

const CONNECTION_TOOLTIP =
  'Live updates connected/disconnected — the UI auto-reconnects and refreshes when events arrive.'

function instanceTooltip(instance: InstanceSummary): string {
  const lastSync = instance.last_polled_at ? relativeTime(instance.last_polled_at) : 'never'
  const lastBackfill = instance.last_backfilled_at ? relativeTime(instance.last_backfilled_at) : 'never'
  return `last sync ${lastSync}; last backfill ${lastBackfill}`
}

/** Presentational: instances (chips with sync/backfill tooltips), queued/processed
 * summary, worker pool size, SSE connection dot, dry-run badge, logs drawer toggle. Data
 * owned/fetched by the parent. */
function StatusHeader({ status, connected, onToggleLogs }: StatusHeaderProps) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 bg-slate-900 px-6 py-3">
      <div className="flex items-center gap-6">
        <span className="text-lg font-semibold tracking-tight text-indigo-400">Impostarr</span>
        {status?.dry_run && (
          <span
            data-testid="dry-run-badge"
            className="rounded border border-amber-500/40 bg-amber-500/15 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-400"
          >
            Dry Run
          </span>
        )}
        <div className="flex flex-wrap gap-2 text-sm text-slate-300">
          {status?.instances.map((instance) => (
            <div
              key={instance.name}
              title={instanceTooltip(instance)}
              className="flex items-center gap-1.5 rounded-full border border-slate-700 bg-slate-800/60 px-2.5 py-1"
            >
              <span className="font-medium text-slate-200">{instance.name}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-5 text-sm text-slate-400">
        <span>
          {status ? `${status.summary.unprocessed} queued · ${status.summary.processed} processed` : '—'}
        </span>
        <span>workers: {status?.workers.pool_size ?? '—'}</span>
        <button
          type="button"
          onClick={onToggleLogs}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Logs
        </button>
        <div className="flex items-center gap-2" title={CONNECTION_TOOLTIP}>
          <span
            data-testid="sse-dot"
            className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-slate-600'}`}
          />
          {connected ? 'connected' : 'disconnected'}
        </div>
      </div>
    </header>
  )
}

export default StatusHeader
