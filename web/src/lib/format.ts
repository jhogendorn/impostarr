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

/** Percentage, 0 decimal places, e.g. "34%". */
export function formatPercent(score: number | null): string {
  return score === null ? '—' : `${Math.round(score * 100)}%`
}

export function pathBasename(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

/** Capitalizes a lowercase status/word for display, e.g. "quarantine" -> "Quarantine". */
export function capitalize(word: string): string {
  return word[0].toUpperCase() + word.slice(1)
}

/** Title-Cases every word in a name, e.g. "main" -> "Main", "backup-2" ->
 * "Backup-2" — for the comparison section's LHS header ("Sonarr {Name}
 * Label"), which Title-Cases the instance name with no surrounding quotes. */
export function titleCase(name: string): string {
  return name
    .split(/(\s|-|_)/)
    .map((part) => (/[a-z0-9]/i.test(part) ? capitalize(part) : part))
    .join('')
}

/** "125s" -> "2m 05s"; used for the active strip's ticking elapsed time. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}m ${String(s).padStart(2, '0')}s`
}

/** ISO timestamp -> "hh:mm:ss" in the viewer's local time, for the log drawer. */
export function formatClock(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString('en-GB', { hour12: false })
}

/** Seconds until expiry -> "3d 04h" / "02h 15m" / "expired", ticking countdown for trash items. */
export function formatCountdown(seconds: number): string {
  if (seconds <= 0) return 'expired'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}d ${String(hours).padStart(2, '0')}h`
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  return `${minutes}m`
}

/** mm:ss timestamp badge for a framegrab, from its `tool_meta.timestamp_s`. */
export function formatTimestampS(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** Zero-padded "SxxEyy" (season/episode both 2-digit, e.g. "S05E03" not
 * "S5E3"), multiple episodes concatenated as "SxxEyyEzz". The one shared
 * formatter for every season/episode render in the inspect panel — no
 * spot should hand-roll `S${season}E${episode}` and skip the padding. */
export function formatSeasonEpisode(season: number, episodes: number[]): string {
  const s = String(season).padStart(2, '0')
  return `S${s}${episodes.map((ep) => `E${String(ep).padStart(2, '0')}`).join('')}`
}
