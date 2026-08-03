import { formatPercent } from '../lib/format'

const BAND_CLASSES = {
  high: 'text-emerald-400',
  mid: 'text-amber-400',
  low: 'text-red-400',
  none: 'text-slate-500',
} as const

function band(score: number | null): keyof typeof BAND_CLASSES {
  if (score === null) return 'none'
  if (score >= 0.8) return 'high'
  if (score >= 0.4) return 'mid'
  return 'low'
}

/** Out-of-flow badge straddling the LHS/RHS boundary (positioned by the
 * parent's zero-width seam column — see ComparisonSection), level with
 * the ident row. Range-coloured (green/amber/red), the percentage number
 * sized to visually match the "CONFIDENCE" caption's rendered width
 * beneath it, with a drop shadow + outline stroke so it reads clearly.
 * Carries its own opaque plate (rounded, bordered, shadowed) rather than
 * floating bare text over whichever panel content it overlaps — it's a
 * badge sitting ON the seam, not transparent text competing with the
 * ident/links underneath it. */
function ConfidenceBadge({ confidence }: { confidence: number | null }) {
  const tone = band(confidence)
  return (
    <div className="absolute left-1/2 top-1/2 z-10 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center whitespace-nowrap rounded-2xl border border-slate-600/70 bg-slate-950/95 px-4 py-2 shadow-2xl">
      <span
        className={`confidence-number text-3xl font-black leading-none ${BAND_CLASSES[tone]}`}
        style={{ WebkitTextStroke: '1px rgba(2,6,23,0.85)' }}
      >
        {formatPercent(confidence)}
      </span>
      <span className="mt-0.5 text-[9px] font-semibold uppercase tracking-[0.3em] text-slate-300">Confidence</span>
    </div>
  )
}

export default ConfidenceBadge
