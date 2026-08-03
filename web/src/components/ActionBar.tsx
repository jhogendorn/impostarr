import { useState, type Dispatch, type SetStateAction } from 'react'
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

const EXPLAINERS: Record<string, string> = {
  markCorrect: 'Confirms this file is the labelled episode. Moves it to Matched.',
  applyRemap: 'Re-links this file to the selected episode in Sonarr. Moves it to Remediated.',
  trashRegrab:
    'Removes this file (a copy is kept in Trash) and asks Sonarr for a replacement. Moves it to Remediated.',
  dismiss: 'Clears the suggested fix. The file stays in Quarantine.',
  ignoreMismatch: 'Stops flagging this file without confirming it. Moves it to Inconclusive.',
  reidentify: 'Runs identification again. The file returns to Pending.',
  park: 'Holds this file so no worker picks it up.',
  unpark: 'Releases this file back to Pending.',
}

const NO_PROPOSAL_EXPLANATION = 'Nothing is proposed for this file. Hover an action to see what it does.'

/** Section B's default explanation — the CURRENT proposed action's
 * explainer, always populated (item 6's "default content"). */
function defaultExplanation(job: JobDetail): string {
  const tone = proposalTone(job)
  if (tone === 'remap') return EXPLAINERS.applyRemap
  if (tone === 'replace') return EXPLAINERS.trashRegrab
  return NO_PROPOSAL_EXPLANATION
}

interface ButtonProps {
  label: string
  tone: ActionTone
  disabled?: boolean
  onClick: () => void
  hoverKey: string
  setActiveKey: Dispatch<SetStateAction<string | null>>
}

function ActionButton({ label, tone, disabled, onClick, hoverKey, setActiveKey }: ButtonProps) {
  const clear = () => setActiveKey((current) => (current === hoverKey ? null : current))
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      onMouseEnter={() => setActiveKey(hoverKey)}
      onMouseLeave={clear}
      onFocus={() => setActiveKey(hoverKey)}
      onBlur={clear}
      className={`${BASE_CONTROL_CLASS} ${TONE_CLASSES[tone]}`}
    >
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
              <ActionButton hoverKey="markCorrect" setActiveKey={setActiveKey}
                  label="Mark Correct"
                  tone="confirm"
                  disabled={pending}
                  onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'is_claimed' }))}
                />

              <ActionButton hoverKey="trashRegrab" setActiveKey={setActiveKey}
                  label="Trash and Regrab"
                  tone="destructive"
                  disabled={pending}
                  onClick={() => void run(() => replaceJob(job.job.id))}
                />

              {showDismiss && (
                <ActionButton hoverKey="dismiss" setActiveKey={setActiveKey} label="Dismiss" tone="neutral" disabled={pending} onClick={() => void run(() => rejectJob(job.job.id))} />
              )}

              <ActionButton hoverKey="ignoreMismatch" setActiveKey={setActiveKey}
                  label="Ignore Mismatch"
                  tone="neutral"
                  disabled={pending}
                  onClick={() => void run(() => postVerdict(job.job.id, { verdict: 'ignore' }))}
                />
            </>
          )}

          {showReidentify && (
            <ActionButton hoverKey="reidentify" setActiveKey={setActiveKey} label="Reidentify" tone="neutral" disabled={pending} onClick={() => void run(() => rerunJob(job.job.id))} />
          )}

          {showPark && (
            <ActionButton hoverKey="park" setActiveKey={setActiveKey} label="Park" tone="neutral" disabled={pending} onClick={() => void run(() => parkJob(job.job.id))} />
          )}

          {showUnpark && (
            <ActionButton hoverKey="unpark" setActiveKey={setActiveKey} label="Unpark" tone="neutral" disabled={pending} onClick={() => void run(() => unparkJob(job.job.id))} />
          )}
        </div>

        <div />

        <div className="justify-self-stretch">
          {showVerdictButtons && (
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
          )}
        </div>
      </div>
      <p className="mt-3 text-sm leading-snug text-slate-300">
        {activeKey ? (EXPLAINERS[activeKey] ?? defaultExplanation(job)) : defaultExplanation(job)}
      </p>
    </div>
  )
}

export default ActionBar
