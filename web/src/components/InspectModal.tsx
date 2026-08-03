import { useEffect, useMemo, useState } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { getJob } from '../api/client'
import type { JobDetail } from '../api/types'
import { collectAlternates, defaultPreviewEpisodeId } from '../lib/inspectData'
import ActionBar from './ActionBar'
import ComparisonSection from './ComparisonSection'
import DatapackSection from './DatapackSection'
import DetailsSection from './DetailsSection'
import PluginResultsSection from './PluginResultsSection'
import ProposedActionBanner from './ProposedActionBanner'

interface InspectModalProps {
  jobId: number | null
  open: boolean
  onClose: () => void
  onChanged: () => void
  dryRun?: boolean
}

/** Inspect panel v4. Content order: (A) proposed-action banner — ALWAYS
 * renders now, even with no proposal (item 5); (B) action bar, whose
 * Apply Remap combobox+confirm-button is the single control for
 * previewing/performing a remap (item 1) — both sticky at the top
 * together; (C) two-column comparison (Sonarr-label/Content-Identity
 * columns, confidence badge, timeline scrubber moved inside the LHS
 * column below Framegrabs — item 4); (D) plugin results; (E) details
 * (fingerprint/probe/remediation log as labelled subsections — item 8);
 * (F) debug datapack. The pre-v3 filename/title/subtitle header row and
 * the "Identification" sentence section are deleted outright — the LHS
 * column's own header/ident rows inside (C) carry that identity
 * information instead (DB links now inline title chips, item 14). A
 * visually-hidden DialogTitle is kept purely for assistive-tech labelling
 * of the dialog itself. `dryRun` is accepted for API-compatibility with
 * existing callers but has no rendering effect — the plain-language
 * dry-run outcome sentence it used to drive was part of the deleted
 * "Identification" section.
 *
 * `selectedEpisodeId` (the Apply Remap preview / RHS "Content Identity"
 * target) is owned here, not by ActionBar or ComparisonSection — they're
 * siblings that both need it (ActionBar's combobox sets it, both it and
 * ComparisonSection's RhsPanel/badge read it), so it's lifted to their
 * common parent. Defaults via `defaultPreviewEpisodeId`, which itself
 * leads with whichever of claimed-vs-alternate is scoring higher (item
 * 16 — previously always defaulted to the top plugin-suggested
 * alternate even when the claimed episode scored higher). */
function InspectModal({ jobId, open, onClose, onChanged }: InspectModalProps) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Apply Remap's preview selection (item 1) — shared by ActionBar's
  // combobox and the RHS "Content Identity" preview, lifted here since
  // ActionBar and ComparisonSection are siblings. Keyed by job id so a
  // manual pick doesn't leak across jobs; otherwise falls back to
  // `defaultPreviewEpisodeId` (items 13 + 16) recomputed fresh each render.
  const [manualSelection, setManualSelection] = useState<{ jobId: number; episodeId: number } | null>(null)

  const labelledIds = useMemo(() => new Set(detail?.file.episode_ids ?? []), [detail])
  const candidates = useMemo(() => (detail ? collectAlternates(detail, labelledIds) : []), [detail, labelledIds])
  const allEpisodes = useMemo(
    () => (detail ? [...(detail.series_episodes ?? [])].sort((a, b) => a.season - b.season || a.episode - b.episode) : []),
    [detail],
  )
  const selectedEpisodeId = detail
    ? manualSelection && manualSelection.jobId === detail.job.id
      ? manualSelection.episodeId
      : defaultPreviewEpisodeId(detail, candidates)
    : null
  function onSelectEpisode(episodeId: number) {
    if (detail) setManualSelection({ jobId: detail.job.id, episodeId })
  }

  useEffect(() => {
    if (!open || jobId === null) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetail(null)
    setLoading(true)
    setError(null)
    getJob(jobId)
      .then((data) => {
        if (!cancelled) setDetail(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, jobId])

  function handleChanged() {
    onChanged()
    if (jobId !== null) {
      getJob(jobId)
        .then(setDetail)
        .catch(() => {})
    }
  }

  return (
    <Dialog open={open} onClose={onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="glow-elevated max-h-[85vh] w-[90vw] max-w-7xl overflow-y-auto rounded-lg bg-slate-900 p-6 text-slate-100">
          <DialogTitle className="sr-only">{`Job #${jobId} inspect panel`}</DialogTitle>

          {/* Sections A + B share this sticky header. */}
          <div className="sticky top-0 z-20 -mx-6 -mt-6 border-b border-slate-800 bg-slate-900 px-6 pb-4 pt-4">
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="absolute right-4 top-3 z-10 rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            >
              ✕
            </button>
            {/* pr-10 clears the absolutely-positioned close button — kept
             * on the banner alone (not the whole header) so ActionBar's
             * own row keeps the DialogPanel's full content width, which
             * ComparisonSection's grid below also uses unmodified: that's
             * what makes ActionBar's Apply Remap group pixel-match the
             * RHS panel's width (item 2/10) — see lib/layout.ts. */}
            <div className="pr-10">{detail && <ProposedActionBanner job={detail} />}</div>
            {detail && selectedEpisodeId !== null && (
              <ActionBar
                job={detail}
                onChanged={handleChanged}
                selectedEpisodeId={selectedEpisodeId}
                onSelectEpisode={onSelectEpisode}
                candidates={candidates}
                allEpisodes={allEpisodes}
              />
            )}
          </div>

          {loading && <p className="mt-4 text-sm text-slate-400">Loading…</p>}
          {error && <p className="mt-4 text-sm text-red-400">{error}</p>}

          {detail && selectedEpisodeId !== null && (
            <div className="mt-6 space-y-6 text-sm">
              <ComparisonSection detail={detail} selectedEpisodeId={selectedEpisodeId} candidates={candidates} />
              <PluginResultsSection detail={detail} />
              <DetailsSection detail={detail} />
              <DatapackSection jobId={detail.job.id} />
            </div>
          )}
        </DialogPanel>
      </div>
    </Dialog>
  )
}

export default InspectModal
