import { formatTimestampS } from '../lib/format'

interface TimelineScrubberProps {
  durationS: number
  valueS: number
  onChange: (seconds: number) => void
}

/** Spans the whole two-column comparison section (rendered above it by
 * ComparisonSection). Dragging fires `onChange`, which the parent fans out
 * to every text panel (to scroll to that time) and the framegrab strip
 * (to highlight the nearest frame) — this component only owns the slider
 * UI, not the sync behaviour itself. */
function TimelineScrubber({ durationS, valueS, onChange }: TimelineScrubberProps) {
  if (durationS <= 0) return null
  return (
    <div className="mb-4 flex items-center gap-3 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2">
      <span className="w-14 shrink-0 font-mono text-xs text-slate-400">{formatTimestampS(valueS)}</span>
      <input
        type="range"
        aria-label="Timeline scrubber"
        min={0}
        max={Math.ceil(durationS)}
        step={1}
        value={Math.min(valueS, durationS)}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-1.5 w-full flex-1 cursor-pointer appearance-none rounded-full bg-slate-700 accent-indigo-500"
      />
      <span className="w-14 shrink-0 text-right font-mono text-xs text-slate-500">{formatTimestampS(durationS)}</span>
    </div>
  )
}

export default TimelineScrubber
