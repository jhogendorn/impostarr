import { useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import {
  ApiError,
  approveJob,
  parkJob,
  postVerdict,
  rejectJob,
  rerunJob,
  replaceJob,
  unparkJob,
} from '../api/client'
import type { EpisodeLabel, JobDetail } from '../api/types'
import { type AlternateCandidate, defaultPreviewEpisodeId, hasProposal, proposalTone } from '../lib/inspectData'
import { formatSeasonEpisode } from '../lib/format'
import { TWO_COLUMN_GRID_CLASS } from '../lib/layout'
import EpisodeCombobox from './EpisodeCombobox'

interface ActionBarProps {
  job: JobDetail
  onChanged: () => void
  selectedEpisodeId: number
  onSelectEpisode: (id: number) => void
  candidates: AlternateCandidate[]
  allEpisodes: EpisodeLabel[]
}

// No remediated→pending edge exists in the backend transition table (a
// remediated job's replacement file arrives as a new discovery instead),
// so Reidentify would always 409 there — omit it. Exported for QueueTable's
// per-row action column and bulk-action eligibility gating. Kept as
// REIDENTIFY_STATUSES (renamed from RERUN_STATUSES — "Rerun" is renamed to
// "Reidentify" throughout the UI; the underlying API client fn/endpoint
// name (`rerunJob`, `POST /jobs/{id}/rerun`) is unchanged, this is a
// display-copy rename only).
export const REIDENTIFY_STATUSES = new Set(['matched', 'error', 'quarantine', 'inconclusive'])

type ActionTone = 'confirm' | 'destructive' | 'remap' | 'neutral'

const TONE_CLASSES: Record<ActionTone, string> = {
  confirm: 'border-emerald-600/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20',
  destructive: 'border-red-600/50 bg-red-500/10 text-red-300 hover:bg-red-500/20',
  remap: 'border-indigo-600/50 bg-indigo-500/10 text-indigo-300 hover:bg-indigo-500/20',
  neutral: 'border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700',
}

const BASE_CONTROL_CLASS = 'rounded-lg border px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40'

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

interface HoverKeyProps {
  hoverKey: string
  setActiveKey: Dispatch<SetStateAction<string | null>>
  children: ReactNode
  /** Defaults to `inline-block` (right for a single button sitting inline
   * in the flex-wrap left group). The Apply Remap group passes `w-full`
   * instead — `inline-block` shrink-to-fits its content, which silently
   * defeats the combobox's `flex-1`/`w-full` sizing (there's no width to
   * fill: the wrapper's own width comes FROM its content in that mode),
   * which is exactly what broke the width-match-to-RHS-panel requirement
   * (item 2) — the combobox+button group rendered ~125px narrower than
   * the RHS panel because of this one wrapper. */
  className?: string
}

/** Wraps one control so hovering/focusing it sets the section-wide "which
 * explainer is showing" key — the explainer text itself renders once, in
 * the permanently-allocated `ActionExplanation` block below the button
 * row (item 6), not per-button. */
function HoverTarget({ hoverKey, setActiveKey, children, className = 'inline-block' }: HoverKeyProps) {
  return (
    <div
      onMouseEnter={() => setActiveKey(hoverKey)}
      onMouseLeave={() => setActiveKey((current) => (current === hoverKey ? null : current))}
      onFocus={() => setActiveKey(hoverKey)}
      onBlur={() => setActiveKey((current) => (current === hoverKey ? null : current))}
      className={className}
    >
      {children}
    </div>
  )
}

const EXPLAINERS: Record<string, string> = {
  markCorrect:
    'Marks this file as verified — positively asserts the label is correct, records gold truth, and feeds the fingerprint corpus. Moves this job to Matched.',
  applyRemap:
    'Preview an episode below, then confirm — remaps the file to it via Sonarr\'s manual import and moves this job to Remediated. Previewing alone never mutates anything.',
  trashRegrab:
    'Moves the current file to Trash (kept for its configured retention window, if any), blocklists the release when possible, and triggers a fresh Sonarr search to regrab a replacement. Moves this job to Remediated.',
  dismiss:
    "Clears the pending proposal. Its main purpose is cancelling a scheduled auto-apply once that feature exists — the job stays in Quarantine either way.",
  ignoreMismatch:
    'Asserts nothing about correctness — moves this job to Inconclusive. No gold record or fingerprint-corpus write (unlike Mark Correct, which positively asserts the label is right).',
  reidentify: 'Resets this job back to Pending for a fresh verification attempt.',
  park: "Pauses this pending job on Hold — the worker pool won't claim it until you Unpark.",
  unpark: 'Releases this held job back to Pending so a worker can claim it.',
}

const NO_PROPOSAL_EXPLANATION = 'No proposed action for this job — choose one of the actions above based on your own review.'

/** Section B's default explanation — the CURRENT proposed action's
 * explainer, always populated (item 6's "default content"). */
function defaultExplanation(job: JobDetail): string {
  const tone = proposalTone(job)
  if (tone === 'remap') return EXPLAINERS.applyRemap
  if (tone === 'replace') return EXPLAINERS.trashRegrab
  return NO_PROPOSAL_EXPLANATION
}

/** Permanent explanation slot (item 6) — occupies real flow space with a
 * fixed min-height so hovering a button swaps its text in place, with no
 * reflow (unlike the earlier absolutely-positioned overlay approach this
 * supersedes). Defaults to the current proposed action's explanation. */
function ActionExplanation({ activeKey, job }: { activeKey: string | null; job: JobDetail }) {
  const text = activeKey ? (EXPLAINERS[activeKey] ?? defaultExplanation(job)) : defaultExplanation(job)
  return <p className="mt-3 min-h-[2.5rem] text-sm leading-snug text-slate-300">{text}</p>
}

interface ButtonProps {
  label: string
  tone: ActionTone
  disabled?: boolean
  onClick: () => void
}

function ActionButton({ label, tone, disabled, onClick }: ButtonProps) {
  return (
    <button type="button" disabled={disabled} onClick={onClick} className={`${BASE_CONTROL_CLASS} ${TONE_CLASSES[tone]}`}>
      {label}
    </button>
  )
}

function findEpisodeLabel(allEpisodes: EpisodeLabel[], job: JobDetail, episodeId: number): EpisodeLabel | undefined {
  return allEpisodes.find((ep) => ep.id === episodeId) ?? job.episode_labels[String(episodeId)]
}

/** State-dependent controls for a job's current status — the inspect
 * panel's Action Bar (section B). Apply Remap is a two-step control
 * (item 1): the combobox only previews a selection (shared with the RHS
 * "Content Identity" preview via `selectedEpisodeId`/`onSelectEpisode`,
 * lifted to InspectModal); a separate button performs the remap, and only
 * once the preview differs from the default (the current proposal/top
 * candidate) does its label change to name the target, making the two
 * steps visually distinct. */
function ActionBar({ job, onChanged, selectedEpisodeId, onSelectEpisode, candidates, allEpisodes }: ActionBarProps) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeKey, setActiveKey] = useState<string | null>(null)

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

  function applyRemap() {
    const label = findEpisodeLabel(allEpisodes, job, selectedEpisodeId)
    if (!label) return
    void run(async () => {
      await postVerdict(job.job.id, { verdict: 'is_other', ident: { season: label.season, episodes: [label.episode] } })
      await approveJob(job.job.id)
    })
  }

  const showVerdictButtons = status === 'quarantine' || status === 'inconclusive'
  const showDismiss = status === 'quarantine' && hasProposal(job)
  const showPark = status === 'pending'
  const showUnpark = status === 'hold'
  const showReidentify = REIDENTIFY_STATUSES.has(status)

  const defaultSelection = defaultPreviewEpisodeId(job, candidates)
  const selectionDiffers = selectedEpisodeId !== defaultSelection
  const selectedLabel = findEpisodeLabel(allEpisodes, job, selectedEpisodeId)
  const remapButtonLabel = selectionDiffers && selectedLabel ? `Apply Remap to ${formatSeasonEpisode(selectedLabel.season, [selectedLabel.episode])}` : 'Apply Remap'

  return (
    <div className="relative">
      {error && (
        <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</p>
      )}
      <div className={TWO_COLUMN_GRID_CLASS}>
        <div className="flex flex-wrap items-center gap-2">
          {showVerdictButtons && (
            <>
              <HoverTarget hoverKey="markCorrect" setActiveKey={setActiveKey}>
                <ActionButton
                  label="Mark Correct"
                  tone="confirm"
                  disabled={pending}
                  onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'is_claimed' }))}
                />
              </HoverTarget>

              <HoverTarget hoverKey="trashRegrab" setActiveKey={setActiveKey}>
                <ActionButton
                  label="Trash and Regrab"
                  tone="destructive"
                  disabled={pending}
                  onClick={() => void run(() => replaceJob(job.job.id))}
                />
              </HoverTarget>

              {showDismiss && (
                <HoverTarget hoverKey="dismiss" setActiveKey={setActiveKey}>
                  <ActionButton label="Dismiss" tone="neutral" disabled={pending} onClick={() => void run(() => rejectJob(job.job.id))} />
                </HoverTarget>
              )}

              <HoverTarget hoverKey="ignoreMismatch" setActiveKey={setActiveKey}>
                <ActionButton
                  label="Ignore Mismatch"
                  tone="neutral"
                  disabled={pending}
                  onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'ignore' }))}
                />
              </HoverTarget>
            </>
          )}

          {showReidentify && (
            <HoverTarget hoverKey="reidentify" setActiveKey={setActiveKey}>
              <ActionButton label="Reidentify" tone="neutral" disabled={pending} onClick={() => void run(() => rerunJob(job.job.id))} />
            </HoverTarget>
          )}

          {showPark && (
            <HoverTarget hoverKey="park" setActiveKey={setActiveKey}>
              <ActionButton label="Park" tone="neutral" disabled={pending} onClick={() => void run(() => parkJob(job.job.id))} />
            </HoverTarget>
          )}

          {showUnpark && (
            <HoverTarget hoverKey="unpark" setActiveKey={setActiveKey}>
              <ActionButton label="Unpark" tone="neutral" disabled={pending} onClick={() => void run(() => unparkJob(job.job.id))} />
            </HoverTarget>
          )}
        </div>

        <div />

        <div className="justify-self-stretch">
          {showVerdictButtons && (
            <HoverTarget hoverKey="applyRemap" setActiveKey={setActiveKey} className="w-full">
              <div className="flex w-full items-stretch gap-2">
                <EpisodeCombobox
                  ariaLabel="Apply Remap"
                  value={selectedEpisodeId}
                  onChange={onSelectEpisode}
                  candidates={candidates}
                  allEpisodes={allEpisodes}
                  episodeLabels={job.episode_labels}
                  disabled={pending}
                  className="min-w-0 flex-1"
                />
                <button
                  type="button"
                  disabled={pending}
                  onClick={applyRemap}
                  className={`${BASE_CONTROL_CLASS} shrink-0 ${TONE_CLASSES.remap}`}
                >
                  {remapButtonLabel}
                </button>
              </div>
            </HoverTarget>
          )}
        </div>
      </div>
      <ActionExplanation activeKey={activeKey} job={job} />
    </div>
  )
}

export default ActionBar
