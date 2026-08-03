import { useEffect, useState } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { getJob } from '../api/client'
import type { JobDetail } from '../api/types'
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

/** Inspect panel v3. Content order: (A) proposed-action banner, (B) action
 * bar — both sticky at the top together; (C) two-column comparison
 * (timeline scrubber + Sonarr-label/Content-Identity columns + confidence
 * badge); (D) plugin results; (E) details (fingerprint/probe/remediation
 * log); (F) debug datapack. The pre-v3 filename/title/subtitle header row
 * and the "Identification" sentence section are deleted outright (per
 * spec) — the LHS column's own header/ident/links rows inside (C) now
 * carry that identity information instead. A visually-hidden DialogTitle
 * is kept purely for assistive-tech labelling of the dialog itself.
 * `dryRun` is accepted for API-compatibility with existing callers but has
 * no v3 rendering effect — the plain-language dry-run outcome sentence it
 * used to drive was part of the deleted "Identification" section. */
function InspectModal({ jobId, open, onClose, onChanged }: InspectModalProps) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
            <div className="pr-10">
              {detail && <ProposedActionBanner job={detail} />}
              {detail && <ActionBar job={detail} onChanged={handleChanged} />}
            </div>
          </div>

          {loading && <p className="mt-4 text-sm text-slate-400">Loading…</p>}
          {error && <p className="mt-4 text-sm text-red-400">{error}</p>}

          {detail && (
            <div className="mt-6 space-y-6 text-sm">
              <ComparisonSection detail={detail} />
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
