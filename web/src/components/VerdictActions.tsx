import { useState, type FormEvent } from 'react'
import { ApiError, approveJob, parkJob, postVerdict, rejectJob, rerunJob, unparkJob } from '../api/client'
import type { JobDetail } from '../api/types'
import { formatSeasonEpisode } from '../lib/format'

interface VerdictActionsProps {
  job: JobDetail
  onChanged: () => void
}

// No remediated→pending edge exists in the backend transition table (a
// remediated job's replacement file arrives as a new discovery instead),
// so Rerun would always 409 there — omit it. Exported for QueueTable's
// per-row action column and bulk-action eligibility gating.
export const RERUN_STATUSES = new Set(['matched', 'error', 'quarantine', 'inconclusive'])

// -- color coding (UI-SPEC section 6): confirm=green, destructive/
// replace=red, remap=indigo, neutral (park/unpark/rerun/dismiss/ignore)=slate.
type ActionTone = 'confirm' | 'destructive' | 'remap' | 'neutral'

const TONE_CLASSES: Record<ActionTone, string> = {
  confirm: 'border-emerald-600/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20',
  destructive: 'border-red-600/50 bg-red-500/10 text-red-300 hover:bg-red-500/20',
  remap: 'border-indigo-600/50 bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/20',
  neutral: 'border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700',
}

const BASE_BUTTON_CLASS = 'rounded-lg border px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40'

/** Resolves proposed-action target episode ids to "S05E03" via
 * `job.episode_labels` (P0.5 — never render bare ids). Falls back to a
 * plain-language "episode(s) 684" only if the resolution is incomplete
 * (e.g. the per-instance episode lookup failed for this job-detail
 * fetch — see routes.py `_episode_labels`). */
function targetLabel(job: JobDetail, targetIds: number[]): string {
  const resolved = targetIds.map((id) => job.episode_labels[String(id)]).filter((label) => label != null)
  if (targetIds.length === 0 || resolved.length !== targetIds.length) {
    return targetIds.length > 0 ? `episode(s) ${targetIds.join(', ')}` : 'the proposed episode'
  }
  return formatSeasonEpisode(resolved[0].season, resolved.map((r) => r.episode))
}

/** Text for the not-yet-approved action, from either an auto-computed
 * `proposed_action` or a human `is_other` verdict's `human_ident` — the
 * latter has no `target_episode_ids` until approve resolves it against
 * Sonarr, so it's described by season/episode numbers instead. */
function describeProposal(job: JobDetail): string | null {
  const verdict = job.verdict
  if (!verdict) return null
  const action = verdict.proposed_action
  if (action !== null && typeof action === 'object') {
    const record = action as Record<string, unknown>
    if (record.kind === 'remap') {
      const ids = record.target_episode_ids
      return `Proposed: remap → ${Array.isArray(ids) ? targetLabel(job, ids as number[]) : 'unknown episode'}`
    }
    if (record.kind === 'replace') {
      return 'Proposed: replace — no known episode of this series matched this file'
    }
  }
  if (verdict.source === 'human' && verdict.human_ident) {
    const { season, episodes } = verdict.human_ident
    return `Proposed: remap → ${formatSeasonEpisode(season, episodes)}`
  }
  return null
}

/** "Apply proposed fix" per spec, but the verb reflects the actual kind
 * being applied ("Apply remap to S05E03" / "Apply replace") so the button
 * itself states its consequence instead of a generic verb. */
function approveButtonLabel(job: JobDetail): string {
  const verdict = job.verdict
  const action = verdict?.proposed_action
  if (action !== null && typeof action === 'object') {
    const record = action as Record<string, unknown>
    if (record.kind === 'remap' && Array.isArray(record.target_episode_ids)) {
      return `Apply remap to ${targetLabel(job, record.target_episode_ids as number[])}`
    }
    if (record.kind === 'replace') return 'Apply replace'
  }
  if (verdict?.source === 'human' && verdict.human_ident) {
    return `Apply remap to ${formatSeasonEpisode(verdict.human_ident.season, verdict.human_ident.episodes)}`
  }
  return 'Apply proposed fix'
}

function approveButtonTone(job: JobDetail): ActionTone {
  const action = job.verdict?.proposed_action
  if (action !== null && typeof action === 'object' && (action as Record<string, unknown>).kind === 'replace') {
    return 'destructive'
  }
  return 'remap'
}

/** Hover/destination-queue explainer for "Apply proposed fix" — names the
 * Trash affordance for replace (the only file-removal action that
 * exists; see Remediator.replace) without inventing any new endpoint. */
function approveExplainer(job: JobDetail): string {
  const action = job.verdict?.proposed_action
  const isReplace = action !== null && typeof action === 'object' && (action as Record<string, unknown>).kind === 'replace'
  if (isReplace) {
    return (
      'Replaces the file via a fresh Sonarr search: the current copy moves to Trash ' +
      '(kept for its configured retention window, if any) before the replacement lands. ' +
      'Moves this job to Remediated.'
    )
  }
  return `Applies the proposed remap via Sonarr's manual import. Moves this job to Remediated.`
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

interface ActionButtonProps {
  label: string
  tone: ActionTone
  explainer: string
  disabled?: boolean
  onClick: () => void
}

/** A single action button with a hover/focus explainer panel. The panel
 * is `absolute` (out of normal flow) so it never reflows the document or
 * preallocates space — it only appears, layered above surrounding
 * content, while the button is hovered or focused. */
function ActionButton({ label, tone, explainer, disabled, onClick }: ActionButtonProps) {
  return (
    <div className="group relative inline-block">
      <button
        type="button"
        disabled={disabled}
        onClick={onClick}
        className={`${BASE_BUTTON_CLASS} ${TONE_CLASSES[tone]}`}
      >
        {label}
      </button>
      <div
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-20 mt-1 w-64 rounded-lg border border-slate-700 bg-slate-950 p-2 text-xs leading-snug text-slate-300 opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {explainer}
      </div>
    </div>
  )
}

/** State-dependent action buttons for a job's current status — the
 * inspect panel's action bar (UI-SPEC section 6: moved to the top,
 * sticky; the sticky/positioning itself is applied by InspectModal's
 * wrapping container, not here). Disables every button while a request
 * is in flight; on success calls `onChanged` so the parent can
 * refetch/close/refresh. */
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
  const proposalText = showApproveReject ? describeProposal(job) : null

  return (
    <div>
      {error && (
        <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </p>
      )}
      {proposalText && <p className="mb-2 text-sm text-slate-400">{proposalText}</p>}
      <div className="flex flex-wrap gap-2">
        {showVerdictButtons && (
          <>
            <ActionButton
              label="Confirm labelled episode"
              tone="confirm"
              disabled={pending}
              explainer="Marks this file as verified. Moves this job to Matched."
              onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'is_claimed' }))}
            />
            <ActionButton
              label="Correct to different episode…"
              tone="remap"
              disabled={pending}
              explainer="Lets you enter the actual season/episode. Submitting keeps this job in Quarantine until you Apply the fix below."
              onClick={() => setShowOtherForm((visible) => !visible)}
            />
            <ActionButton
              label="Mark ignored"
              tone="neutral"
              disabled={pending}
              explainer="Moves this job to Inconclusive without applying any fix. (Different from Dismiss proposal below, which clears a proposed fix but leaves the job in Quarantine.)"
              onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'ignore' }))}
            />
          </>
        )}
        {showApproveReject && (
          <>
            <ActionButton
              label={approveButtonLabel(job)}
              tone={approveButtonTone(job)}
              disabled={pending}
              explainer={approveExplainer(job)}
              onClick={() => void run(() => approveJob(job.job.id))}
            />
            <ActionButton
              label="Dismiss proposal"
              tone="neutral"
              disabled={pending}
              explainer="Clears this proposed fix only. The job stays in Quarantine for further review. (Different from Mark ignored above, which moves the job to Inconclusive.)"
              onClick={() => void run(() => rejectJob(job.job.id))}
            />
          </>
        )}
        {showPark && (
          <ActionButton
            label="Park"
            tone="neutral"
            disabled={pending}
            explainer="Pauses this pending job on Hold — the worker pool won't claim it until you Unpark."
            onClick={() => void run(() => parkJob(job.job.id))}
          />
        )}
        {showUnpark && (
          <ActionButton
            label="Unpark"
            tone="neutral"
            disabled={pending}
            explainer="Releases this held job back to Pending so a worker can claim it."
            onClick={() => void run(() => unparkJob(job.job.id))}
          />
        )}
        {showRerun && (
          <ActionButton
            label="Rerun"
            tone="neutral"
            disabled={pending}
            explainer="Resets this job back to Pending for a fresh verification attempt."
            onClick={() => void run(() => rerunJob(job.job.id))}
          />
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
          <button
            type="submit"
            disabled={pending}
            className={`${BASE_BUTTON_CLASS} ${TONE_CLASSES.remap}`}
          >
            Submit
          </button>
        </form>
      )}
    </div>
  )
}

export default VerdictActions
