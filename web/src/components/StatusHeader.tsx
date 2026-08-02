import type { StatusResponse } from '../api/types'

interface StatusHeaderProps {
  status: StatusResponse | null
  connected: boolean
}

/** Presentational: instances + watermarks, queue-depth summary, worker
 * pool size, SSE connection dot. Data owned/fetched by the parent. */
function StatusHeader({ status, connected }: StatusHeaderProps) {
  const totalQueued = status ? Object.values(status.queues).reduce((sum, n) => sum + n, 0) : null

  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 bg-slate-900 px-6 py-3">
      <div className="flex items-center gap-6">
        <span className="text-lg font-semibold tracking-tight text-indigo-400">Impostarr</span>
        <div className="flex flex-wrap gap-4 text-sm text-slate-300">
          {status?.instances.map((instance) => (
            <div key={instance.name} className="flex items-center gap-1.5">
              <span className="font-medium text-slate-200">{instance.name}</span>
              <span className="text-slate-500">
                {instance.history_watermark ? `watermark ${instance.history_watermark}` : 'no watermark'}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-5 text-sm text-slate-400">
        <span>queued: {totalQueued ?? '—'}</span>
        <span>workers: {status?.workers.pool_size ?? '—'}</span>
        <div className="flex items-center gap-2">
          <span
            data-testid="sse-dot"
            className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-slate-600'}`}
            title={connected ? 'connected' : 'disconnected'}
          />
          {connected ? 'connected' : 'disconnected'}
        </div>
      </div>
    </header>
  )
}

export default StatusHeader
