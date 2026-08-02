import { useState, type FormEvent } from 'react'
import { ApiError, approveJob, parkJob, postVerdict, rejectJob, rerunJob, unparkJob } from '../api/client'
import type { JobDetail } from '../api/types'

interface VerdictActionsProps {
  job: JobDetail
  onChanged: () => void
}

// No remediated→pending edge exists in the backend transition table (a
// remediated job's replacement file arrives as a new discovery instead),
// so Rerun would always 409 there — omit it. Exported for QueueTable's
// per-row action column and bulk-action eligibility gating.
export const RERUN_STATUSES = new Set(['matched', 'error', 'quarantine', 'inconclusive'])

const BUTTON_CLASS =
  'rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40'

/** Text for the not-yet-approved action, from either an auto-computed
 * `proposed_action` or a human `is_other` verdict's `human_ident` — the
 * latter has no `target_episode_ids` until approve resolves it against
 * Sonarr, so it's described by season/episode numbers instead. */
function describeProposal(verdict: JobDetail['verdict']): string | null {
  if (!verdict) return null
  const action = verdict.proposed_action
  if (action !== null && typeof action === 'object') {
    const record = action as Record<string, unknown>
    if (record.kind === 'remap') {
      const ids = record.target_episode_ids
      return `Proposed: remap → episodes ${Array.isArray(ids) ? ids.join(', ') : '?'}`
    }
    if (record.kind === 'replace') return 'Proposed: replace'
  }
  if (verdict.source === 'human' && verdict.human_ident) {
    const { season, episodes } = verdict.human_ident
    return `Proposed: remap → S${season}E${episodes.join(',')}`
  }
  return null
}

function formatError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : JSON.stringify(body)
    return `${err.status}: ${detail}`
  }
  return err instanceof Error ? err.message : String(err)
}

/** State-dependent action buttons for a job's current status. Disables
 * every button while a request is in flight; on success calls
 * `onChanged` so the parent can refetch/close/refresh. */
function VerdictActions({ job, onChanged }: VerdictActionsProps) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showOtherForm, setShowOtherForm] = useState(false)
  const [season, setSeason] = useState('')
  const [episodes, setEpisodes] = useState('')

  const status = job.job.status

  async function run(action: () => Promise<unknown>) {
    setPending(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setPending(false)
    }
  }

  function submitIsOther(event: FormEvent) {
    event.preventDefault()
    const seasonNumber = Number(season)
    const episodeNumbers = episodes
      .split(',')
      .map((value) => Number(value.trim()))
      .filter((value) => !Number.isNaN(value))
    if (Number.isNaN(seasonNumber) || episodeNumbers.length === 0) {
      setError('enter a season and at least one episode number')
      return
    }
    void run(() =>
      postVerdict(job.job.id, { verdict: 'is_other', ident: { season: seasonNumber, episodes: episodeNumbers } }),
    )
  }

  const showVerdictButtons = status === 'quarantine' || status === 'inconclusive'
  const showApproveReject =
    status === 'quarantine' &&
    (job.verdict?.proposed_action != null || (job.verdict?.source === 'human' && job.verdict?.human_ident != null))
  const showPark = status === 'pending'
  const showUnpark = status === 'hold'
  const showRerun = RERUN_STATUSES.has(status)
  const proposalText = showApproveReject ? describeProposal(job.verdict) : null

  return (
    <div className="border-t border-slate-800 pt-4">
      {error && (
        <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </p>
      )}
      {proposalText && <p className="mb-2 text-sm text-slate-400">{proposalText}</p>}
      <div className="flex flex-wrap gap-2">
        {showVerdictButtons && (
          <>
            <button
              type="button"
              disabled={pending}
              onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'is_claimed' }))}
              className={BUTTON_CLASS}
            >
              Is claimed episode
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => setShowOtherForm((visible) => !visible)}
              className={BUTTON_CLASS}
            >
              Is other…
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'ignore' }))}
              className={BUTTON_CLASS}
            >
              Ignore
            </button>
          </>
        )}
        {showApproveReject && (
          <>
            <button
              type="button"
              disabled={pending}
              onClick={() => void run(() => approveJob(job.job.id))}
              className={BUTTON_CLASS}
            >
              Approve action
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => void run(() => rejectJob(job.job.id))}
              className={BUTTON_CLASS}
            >
              Reject
            </button>
          </>
        )}
        {showPark && (
          <button
            type="button"
            disabled={pending}
            onClick={() => void run(() => parkJob(job.job.id))}
            className={BUTTON_CLASS}
          >
            Park
          </button>
        )}
        {showUnpark && (
          <button
            type="button"
            disabled={pending}
            onClick={() => void run(() => unparkJob(job.job.id))}
            className={BUTTON_CLASS}
          >
            Unpark
          </button>
        )}
        {showRerun && (
          <button
            type="button"
            disabled={pending}
            onClick={() => void run(() => rerunJob(job.job.id))}
            className={BUTTON_CLASS}
          >
            Rerun
          </button>
        )}
      </div>
      {showVerdictButtons && showOtherForm && (
        <form onSubmit={submitIsOther} className="mt-3 flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-400">
            Season
            <input
              value={season}
              onChange={(event) => setSeason(event.target.value)}
              className="mt-1 block w-20 rounded-lg border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-200"
            />
          </label>
          <label className="text-xs text-slate-400">
            Episodes (CSV)
            <input
              value={episodes}
              onChange={(event) => setEpisodes(event.target.value)}
              placeholder="1, 2, 3"
              className="mt-1 block w-40 rounded-lg border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-200"
            />
          </label>
          <button type="submit" disabled={pending} className={BUTTON_CLASS}>
            Submit
          </button>
        </form>
      )}
    </div>
  )
}

export default VerdictActions
