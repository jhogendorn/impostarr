import { useEffect, useState } from 'react'
import { parkJob, rerunJob, unparkJob } from '../api/client'
import type { InstanceSummary, JobStatus, JobSummary, QueuePage, QueueSortField, SortDir } from '../api/types'
import { capitalize, formatPercent, pathBasename, relativeTime, scoreBandClass } from '../lib/format'
import { RERUN_STATUSES } from './VerdictActions'

const PAGE_SIZE_OPTIONS = [25, 50, 100, 200] as const

type BulkAction = 'rerun' | 'park' | 'unpark'

const BULK_ACTION_FNS: Record<BulkAction, (id: number) => Promise<unknown>> = {
  rerun: rerunJob,
  park: parkJob,
  unpark: unparkJob,
}

const BULK_ACTION_LABELS: Record<BulkAction, string> = {
  rerun: 'Rerun',
  park: 'Park',
  unpark: 'Unpark',
}

function isEligible(action: BulkAction, status: JobStatus): boolean {
  if (action === 'rerun') return RERUN_STATUSES.has(status)
  if (action === 'park') return status === 'pending'
  return status === 'hold' // unpark
}

interface RowActionsProps {
  job: JobSummary
  onChanged: () => void
}

/** Contextual per-row action buttons, same eligibility rules as
 * VerdictActions' park/unpark/rerun buttons in the inspect modal. No Inspect
 * button here — the row itself is already a click target for that. */
function RowActions({ job, onChanged }: RowActionsProps) {
  const [pending, setPending] = useState(false)

  async function run(action: () => Promise<unknown>) {
    setPending(true)
    try {
      await action()
      onChanged()
    } catch (err) {
      console.error('row action failed', err)
    } finally {
      setPending(false)
    }
  }

  const buttonClass =
    'rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40'

  return (
    <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
      {RERUN_STATUSES.has(job.status) && (
        <button type="button" disabled={pending} className={buttonClass} onClick={() => void run(() => rerunJob(job.job_id))}>
          Rerun
        </button>
      )}
      {job.status === 'pending' && (
        <button type="button" disabled={pending} className={buttonClass} onClick={() => void run(() => parkJob(job.job_id))}>
          Park
        </button>
      )}
      {job.status === 'hold' && (
        <button type="button" disabled={pending} className={buttonClass} onClick={() => void run(() => unparkJob(job.job_id))}>
          Unpark
        </button>
      )}
    </div>
  )
}

interface SortableThProps {
  field: QueueSortField
  label: string
  sortField: QueueSortField
  sortDir: SortDir
  onSortChange: (field: QueueSortField, dir: SortDir) => void
}

/** A clickable column header: click cycles asc/desc when it's already the
 * active sort field, or switches to this field (defaulting to desc) when
 * it isn't. The active field gets a bright ▲/▼ arrow; other sortable
 * columns get a dim ⇅ so it's visually obvious they're clickable too. */
function SortableTh({ field, label, sortField, sortDir, onSortChange }: SortableThProps) {
  const active = field === sortField
  return (
    <th className="px-3 py-2">
      <button
        type="button"
        onClick={() => onSortChange(field, active ? (sortDir === 'asc' ? 'desc' : 'asc') : 'desc')}
        className="flex items-center gap-1 uppercase tracking-wide text-slate-500 hover:text-slate-300"
      >
        {label}
        <span className={active ? 'text-slate-200' : 'text-slate-700'}>{active ? (sortDir === 'asc' ? '▲' : '▼') : '⇅'}</span>
      </button>
    </th>
  )
}

interface QueueTableProps {
  page: QueuePage | null
  pageIndex: number
  pageSize: number
  onPageChange: (pageIndex: number) => void
  onPageSizeChange: (pageSize: number) => void
  onInspect: (jobId: number) => void
  instances: InstanceSummary[]
  selectedInstance: string | undefined
  onInstanceChange: (instance: string | undefined) => void
  sortField: QueueSortField
  sortDir: SortDir
  onSortChange: (field: QueueSortField, dir: SortDir) => void
  onChanged: () => void
}

/** Paged rows for the active queue tab. Data + paging/filter-by-instance/sort
 * state owned by the parent (App), which refetches on change and on
 * debounced SSE `job_update` events. Free-text filtering, row selection, and
 * bulk actions are client-local to this component. */
function QueueTable({
  page,
  pageIndex,
  pageSize,
  onPageChange,
  onPageSizeChange,
  onInspect,
  instances,
  selectedInstance,
  onInstanceChange,
  sortField,
  sortDir,
  onSortChange,
  onChanged,
}: QueueTableProps) {
  const [filterText, setFilterText] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkProgress, setBulkProgress] = useState<{ action: BulkAction; done: number; total: number } | null>(null)
  const [bulkErrors, setBulkErrors] = useState<string[]>([])

  // A freshly-fetched page is a new object identity — reset selection so
  // stale row ids from a previous page/tab/filter don't linger selected.
  useEffect(() => {
    setSelected(new Set())
  }, [page])

  const allItems = page?.items ?? []
  const total = page?.total ?? 0
  const rangeStart = total === 0 ? 0 : (pageIndex - 1) * pageSize + 1
  const rangeEnd = Math.min(pageIndex * pageSize, total)
  const hasNext = pageIndex * pageSize < total

  const needle = filterText.trim().toLowerCase()
  const items = needle
    ? allItems.filter(
        (job) =>
          job.file.sonarr_path.toLowerCase().includes(needle) || String(job.file.series_id).includes(needle),
      )
    : allItems

  function toggleRow(jobId: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  function toggleAll() {
    setSelected((prev) => (prev.size === items.length ? new Set() : new Set(items.map((job) => job.job_id))))
  }

  async function runBulk(action: BulkAction) {
    const eligible = items.filter((job) => selected.has(job.job_id) && isEligible(action, job.status))
    if (eligible.length === 0) return
    setBulkErrors([])
    setBulkProgress({ action, done: 0, total: eligible.length })
    const fn = BULK_ACTION_FNS[action]
    const errors: string[] = []
    for (const job of eligible) {
      try {
        await fn(job.job_id)
      } catch (err) {
        errors.push(`job ${job.job_id}: ${err instanceof Error ? err.message : String(err)}`)
      }
      setBulkProgress((prev) => (prev ? { ...prev, done: prev.done + 1 } : prev))
    }
    setBulkProgress(null)
    setBulkErrors(errors)
    setSelected(new Set())
    onChanged()
  }

  const selectedJobs = items.filter((job) => selected.has(job.job_id))

  return (
    <div className="px-6 py-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        {instances.length > 1 && (
          <select
            aria-label="Instance"
            value={selectedInstance ?? ''}
            onChange={(e) => onInstanceChange(e.target.value || undefined)}
            className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-200"
          >
            <option value="">All instances</option>
            {instances.map((instance) => (
              <option key={instance.name} value={instance.name}>
                {instance.name}
              </option>
            ))}
          </select>
        )}
        <input
          type="text"
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          placeholder="Filter by path or series id…"
          className="min-w-56 flex-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-200 placeholder:text-slate-500"
        />
      </div>

      {/* Fixed-height slot, always rendered — its contents (below) appear
       * only when something is selected, but the slot itself never does,
       * so selecting/deselecting rows causes zero vertical layout shift. */}
      <div
        role="group"
        aria-label="Bulk actions"
        className={`mb-3 flex h-10 flex-wrap items-center gap-3 rounded-lg px-3 text-sm text-slate-200 ${
          selected.size > 0 ? 'border border-indigo-500/30 bg-indigo-500/10 py-2' : ''
        }`}
      >
        {selected.size > 0 && (
          <>
            <span>{selected.size} selected</span>
            {(['rerun', 'park', 'unpark'] as const).map((action) => {
              const eligibleCount = selectedJobs.filter((job) => isEligible(action, job.status)).length
              return (
                <button
                  key={action}
                  type="button"
                  disabled={eligibleCount === 0 || bulkProgress !== null}
                  onClick={() => void runBulk(action)}
                  className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {BULK_ACTION_LABELS[action]} ({eligibleCount})
                </button>
              )
            })}
            {bulkProgress && (
              <span className="text-xs text-slate-400">
                {BULK_ACTION_LABELS[bulkProgress.action]}ing {bulkProgress.done}/{bulkProgress.total}…
              </span>
            )}
          </>
        )}
      </div>
      {bulkErrors.length > 0 && (
        <div className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {bulkErrors.map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      )}

      <table className="w-full border-separate border-spacing-y-1 text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="px-3 py-1" />
            <th className="px-3 py-1" colSpan={2}>
              Labelled episode
            </th>
            <th className="px-3 py-1" />
            <th className="px-3 py-1" />
            <th className="px-3 py-1" />
            <th className="px-3 py-1" />
            <th className="px-3 py-1" />
            <th className="px-3 py-1" />
          </tr>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="px-3 py-2">
              <input
                type="checkbox"
                aria-label="Select all"
                checked={items.length > 0 && selected.size === items.length}
                disabled={items.length === 0}
                onChange={toggleAll}
                onClick={(e) => e.stopPropagation()}
                className="h-4 w-4 cursor-pointer accent-indigo-500 disabled:cursor-not-allowed disabled:opacity-40"
              />
            </th>
            <SortableTh field="series" label="Series (id)" sortField={sortField} sortDir={sortDir} onSortChange={onSortChange} />
            <th className="px-3 py-2">Episode(s)</th>
            <th className="px-3 py-2">File</th>
            <SortableTh field="instance" label="Instance" sortField={sortField} sortDir={sortDir} onSortChange={onSortChange} />
            <SortableTh field="confidence" label="Confidence" sortField={sortField} sortDir={sortDir} onSortChange={onSortChange} />
            <th className="px-3 py-2">Outcome</th>
            <SortableTh field="updated_at" label="Updated" sortField={sortField} sortDir={sortDir} onSortChange={onSortChange} />
            <th className="px-3 py-2">Actions</th>
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
                <td className="rounded-l-lg px-3 py-2" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    aria-label={`Select job ${job.job_id}`}
                    checked={selected.has(job.job_id)}
                    onChange={() => toggleRow(job.job_id)}
                    onClick={(e) => e.stopPropagation()}
                    className="h-4 w-4 cursor-pointer accent-indigo-500"
                  />
                </td>
                <td className="px-3 py-2 text-slate-200">Series {job.file.series_id}</td>
                <td className="px-3 py-2 text-slate-400">{job.file.episode_ids.join(', ')}</td>
                <td className="px-3 py-2 text-slate-400">{pathBasename(job.file.sonarr_path)}</td>
                <td className="px-3 py-2 text-slate-400">{job.instance ?? '—'}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded border px-1.5 py-0.5 text-xs font-medium ${scoreBandClass(score)}`}
                  >
                    {formatPercent(score)}
                  </span>
                </td>
                <td className="px-3 py-2 text-slate-400">{capitalize(job.status)}</td>
                <td className="px-3 py-2 text-slate-500">{relativeTime(job.updated_at)}</td>
                <td className="rounded-r-lg px-3 py-2">
                  <RowActions job={job} onChanged={onChanged} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {items.length === 0 && <p className="px-3 py-6 text-sm text-slate-500">No jobs in this queue.</p>}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-slate-400">
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Page size
            <select
              aria-label="Page size"
              value={pageSize}
              onChange={(e) => onPageSizeChange(Number(e.target.value))}
              className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-slate-200"
            >
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
          <span>
            {rangeStart}–{rangeEnd} of {total}
          </span>
        </div>
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
