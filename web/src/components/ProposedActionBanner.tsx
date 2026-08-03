import { useEffect, useState } from 'react'
import type { JobDetail } from '../api/types'
import { formatCountdown } from '../lib/format'
import { describeProposal, proposalTone } from '../lib/inspectData'

const TONE_CLASSES: Record<'remap' | 'replace', string> = {
  remap: 'border-indigo-600/40 bg-indigo-500/10 text-indigo-200',
  replace: 'border-red-600/40 bg-red-500/10 text-red-200',
}

function RemapIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4 shrink-0">
      <path d="M4 8a6 6 0 0 1 10.5-3.9M4 8V3.5M4 8h4.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M16 12a6 6 0 0 1-10.5 3.9M16 12v4.5M16 12h-4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function ReplaceIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4 shrink-0">
      <path d="M4 6h12M8 6V4.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1V6M6 6l.6 9a1 1 0 0 0 1 .9h4.8a1 1 0 0 0 1-.9L14 6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Ticking "Auto Apply In: <countdown>" — only ever renders once a verdict
 * carries `apply_at` (not shipped yet, see task #13; this path is
 * exercised by tests constructing a fixture with the field set). Falls
 * back to a static "Manual Review" slot otherwise. */
function AutoApplySlot({ applyAt }: { applyAt: string | null }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!applyAt) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [applyAt])

  if (!applyAt) {
    return <span className="shrink-0 text-xs font-medium uppercase tracking-wide text-slate-400">Manual Review</span>
  }
  const secondsLeft = (new Date(applyAt).getTime() - now) / 1000
  return (
    <span className="shrink-0 text-xs font-medium uppercase tracking-wide text-slate-200">
      Auto Apply In: {formatCountdown(secondsLeft)}
    </span>
  )
}

/** Section A: the proposed-action banner — only rendered when a proposal
 * (auto-computed `proposed_action`) or a human `is_other` verdict exists.
 * Tinted to match the action bar's Apply Remap (indigo) / Trash and
 * Regrab (red) button colours, so the banner visually previews which
 * control would carry it out. */
function ProposedActionBanner({ job }: { job: JobDetail }) {
  const tone = proposalTone(job)
  if (!tone) return null
  const description = describeProposal(job)

  return (
    <div className={`mb-3 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 ${TONE_CLASSES[tone]}`}>
      <div className="flex min-w-0 items-center gap-2 text-sm">
        {tone === 'remap' ? <RemapIcon /> : <ReplaceIcon />}
        <span className="truncate">{description ?? 'Proposed action pending review'}</span>
      </div>
      <AutoApplySlot applyAt={job.verdict?.apply_at ?? null} />
    </div>
  )
}

export default ProposedActionBanner
