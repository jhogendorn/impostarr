import { useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import {
  ApiError,
  approveJob,
  parkJob,
  postVerdict,
  rejectJob,
  replaceJob,
  rerunJob,
  unparkJob,
} from '../api/client'
import type { EpisodeLabel, JobDetail } from '../api/types'
import { collectAlternates, episodeOptionLabel, hasProposal } from '../lib/inspectData'

interface ActionBarProps {
  job: JobDetail
  onChanged: () => void
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
}

/** Wraps one control so hovering/focusing it sets the section-wide
 * "which explainer is showing" key — the actual explainer panel is
 * rendered ONCE, at the action bar's bottom edge (see ExplainerOverlay),
 * not per-button, so it can overlay the section below without any one
 * button owning its own floating tooltip. */
function HoverTarget({ hoverKey, setActiveKey, children }: HoverKeyProps) {
  return (
    <div
      onMouseEnter={() => setActiveKey(hoverKey)}
      onMouseLeave={() => setActiveKey((current) => (current === hoverKey ? null : current))}
      onFocus={() => setActiveKey(hoverKey)}
      onBlur={() => setActiveKey((current) => (current === hoverKey ? null : current))}
      className="inline-block"
    >
      {children}
    </div>
  )
}

const EXPLAINERS: Record<string, string> = {
  markCorrect:
    'Marks this file as verified — positively asserts the label is correct, records gold truth, and feeds the fingerprint corpus. Moves this job to Matched.',
  applyRemap:
    "Choose the correct episode from the list. Selecting one immediately remaps the file to it via Sonarr's manual import and moves this job to Remediated.",
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

/** The single explainer surface for the whole action bar — absolutely
 * positioned so it overlays whatever renders below the (sticky) action
 * bar rather than pushing it down. Height-animated via max-height/opacity,
 * not `display`, so the transition itself never reflows either. */
function ExplainerOverlay({ activeKey }: { activeKey: string | null }) {
  const text = activeKey ? EXPLAINERS[activeKey] : null
  return (
    <div
      role="tooltip"
      aria-hidden={!text}
      className={`pointer-events-none absolute inset-x-0 top-full z-30 overflow-hidden border-t border-slate-700 bg-slate-950 px-4 text-sm leading-snug text-slate-300 shadow-xl transition-[max-height,opacity] duration-150 ${
        text ? 'max-h-24 opacity-100 py-3' : 'max-h-0 opacity-0 py-0'
      }`}
    >
      {text}
    </div>
  )
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

/** Builds the two option groups shared by the Apply Remap dropdown:
 * Candidates (plugin-suggested in-series alternates, descending
 * confidence) then All Episodes (every episode of the series, from
 * `job.series_episodes` — see routes.py `get_job_detail`). */
function buildEpisodeGroups(job: JobDetail) {
  const labelledIds = new Set(job.file.episode_ids)
  const candidates = collectAlternates(job, labelledIds)
  const allEpisodes = [...(job.series_episodes ?? [])].sort((a, b) => a.season - b.season || a.episode - b.episode)
  return { candidates, allEpisodes }
}

function findEpisodeLabel(job: JobDetail, episodeId: number): EpisodeLabel | undefined {
  return (job.series_episodes ?? []).find((ep) => ep.id === episodeId) ?? job.episode_labels[String(episodeId)]
}

/** State-dependent controls for a job's current status — the inspect
 * panel's Action Bar (section B). Renders inline with (and overlaid by)
 * a single shared explainer surface at the bar's bottom edge — see
 * ExplainerOverlay. */
function ActionBar({ job, onChanged }: ActionBarProps) {
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

  function applyRemapTo(episodeId: number) {
    const label = findEpisodeLabel(job, episodeId)
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

  const { candidates, allEpisodes } = buildEpisodeGroups(job)

  return (
    <div className="relative">
      {error && (
        <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</p>
      )}
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

            <HoverTarget hoverKey="applyRemap" setActiveKey={setActiveKey}>
              <select
                aria-label="Apply Remap"
                disabled={pending}
                value=""
                onChange={(event) => {
                  const id = Number(event.target.value)
                  if (!Number.isNaN(id) && id > 0) applyRemapTo(id)
                }}
                className={`${BASE_CONTROL_CLASS} ${TONE_CLASSES.remap}`}
              >
                <option value="" disabled>
                  Apply Remap
                </option>
                {candidates.length > 0 && (
                  <optgroup label="Candidates">
                    {candidates.map((c) => {
                      const epId = c.episodeIds[0]
                      const label = findEpisodeLabel(job, epId)
                      return (
                        <option key={epId} value={epId}>
                          {label ? episodeOptionLabel(label) : `episode ${epId}`}
                        </option>
                      )
                    })}
                  </optgroup>
                )}
                <optgroup label="All Episodes">
                  {allEpisodes.map((ep) => (
                    <option key={ep.id} value={ep.id}>
                      {episodeOptionLabel(ep)}
                    </option>
                  ))}
                </optgroup>
              </select>
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
      <ExplainerOverlay activeKey={activeKey} />
    </div>
  )
}

export default ActionBar
