/** Tiny formatting helpers shared by queue/inspect components (no date lib). */

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

/** Formats an ISO timestamp as "3m ago" / "in 2h" style relative text. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const diffSeconds = (then - now) / 1000
  const absSeconds = Math.abs(diffSeconds)
  if (absSeconds < 60) return 'just now'
  for (const [unit, secondsInUnit] of UNITS) {
    if (absSeconds >= secondsInUnit) {
      return rtf.format(Math.round(diffSeconds / secondsInUnit), unit)
    }
  }
  return rtf.format(Math.round(diffSeconds / 60), 'minute')
}

export type ScoreBand = 'high' | 'mid' | 'low' | 'none'

/** Score band per spec thresholds: >=0.8 high, 0.4-0.8 mid, <0.4 low, null none. */
export function scoreBand(score: number | null): ScoreBand {
  if (score === null) return 'none'
  if (score >= 0.8) return 'high'
  if (score >= 0.4) return 'mid'
  return 'low'
}

const SCORE_BAND_CLASSES: Record<ScoreBand, string> = {
  high: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  mid: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  low: 'bg-red-500/15 text-red-400 border-red-500/30',
  none: 'bg-slate-500/15 text-slate-400 border-slate-500/30',
}

export function scoreBandClass(score: number | null): string {
  return SCORE_BAND_CLASSES[scoreBand(score)]
}

export function formatScore(score: number | null): string {
  return score === null ? '—' : score.toFixed(2)
}

export function pathBasename(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}
