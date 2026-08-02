import type { QueuePage } from '../api/types'
import { formatScore, pathBasename, relativeTime, scoreBandClass } from '../lib/format'

interface QueueTableProps {
  page: QueuePage | null
  pageIndex: number
  pageSize: number
  onPageChange: (pageIndex: number) => void
  onInspect: (jobId: number) => void
}

/** Paged rows for the active queue tab. Presentational — data + paging
 * state owned by the parent (App), which refetches on tab/page change and
 * on debounced SSE `job_update` events. */
function QueueTable({ page, pageIndex, pageSize, onPageChange, onInspect }: QueueTableProps) {
  const items = page?.items ?? []
  const total = page?.total ?? 0
  const rangeStart = total === 0 ? 0 : (pageIndex - 1) * pageSize + 1
  const rangeEnd = Math.min(pageIndex * pageSize, total)
  const hasNext = pageIndex * pageSize < total

  return (
    <div className="px-6 py-4">
      <table className="w-full border-separate border-spacing-y-1 text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="px-3 py-2">Series</th>
            <th className="px-3 py-2">File</th>
            <th className="px-3 py-2">Episodes</th>
            <th className="px-3 py-2">Score</th>
            <th className="px-3 py-2">Outcome</th>
            <th className="px-3 py-2">Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((job) => {
            const score = job.verdict?.s_claimed ?? null
            return (
              <tr
                key={job.job_id}
                onClick={() => onInspect(job.job_id)}
                className="cursor-pointer rounded-lg bg-slate-800/60 hover:bg-slate-800"
              >
                <td className="rounded-l-lg px-3 py-2 text-slate-200">{job.file.series_id}</td>
                <td className="px-3 py-2 text-slate-300">{pathBasename(job.file.sonarr_path)}</td>
                <td className="px-3 py-2 text-slate-400">{job.file.episode_ids.join(', ')}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded border px-1.5 py-0.5 text-xs font-medium ${scoreBandClass(score)}`}
                  >
                    {formatScore(score)}
                  </span>
                </td>
                <td className="px-3 py-2 text-slate-400">{job.verdict?.outcome ?? '—'}</td>
                <td className="rounded-r-lg px-3 py-2 text-slate-500">{relativeTime(job.updated_at)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {items.length === 0 && <p className="px-3 py-6 text-sm text-slate-500">No jobs in this queue.</p>}
      <div className="mt-3 flex items-center justify-between text-sm text-slate-400">
        <span>
          {rangeStart}–{rangeEnd} of {total}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={pageIndex <= 1}
            onClick={() => onPageChange(pageIndex - 1)}
            className="rounded-lg border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            Prev
          </button>
          <button
            type="button"
            disabled={!hasNext}
            onClick={() => onPageChange(pageIndex + 1)}
            className="rounded-lg border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  )
}

export default QueueTable
