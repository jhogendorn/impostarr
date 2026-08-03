import { useEffect, useMemo, useRef, useState } from 'react'
import type { TimedCue } from '../api/types'
import { formatTimestampS } from '../lib/format'

export interface TextPanelSource {
  label: string
  cues: TimedCue[]
}

interface TextPanelProps {
  title: string
  sources: TextPanelSource[]
  emptyText: string
  /** Current scrubber position (seconds), or `null` before the user has
   * dragged the timeline scrubber — drives the nearest-cue highlight and
   * auto-scroll within this panel's own scrollable container only. */
  scrubTimeS?: number | null
  /** Whether to show the source-picker when more than one source exists.
   * Defaults `true`. Embedded Subtitles sets this `false` (item 12): full
   * per-track language handling is deferred, so it always shows just the
   * first extracted track rather than reintroducing a many-option picker. */
  showSourceSelector?: boolean
}

/** One independently-scrollable text panel (embedded subs / transcript /
 * reference subs), taller than v2 (~400px) — with a source selector shown
 * only when more than one source exists (e.g. multiple embedded-subtitle
 * language tracks), per-line hover tooltips showing that line's
 * timestamp, and nearest-cue highlight/auto-scroll driven by the shared
 * timeline scrubber. */
function TextPanel({ title, sources, emptyText, scrubTimeS = null, showSourceSelector = true }: TextPanelProps) {
  const [index, setIndex] = useState(0)
  const active = sources[Math.min(index, sources.length - 1)]
  const containerRef = useRef<HTMLDivElement>(null)
  const cueRefs = useRef<(HTMLParagraphElement | null)[]>([])

  const nearestIdx = useMemo(() => {
    if (scrubTimeS == null || !active) return null
    let best = -1
    let bestDiff = Infinity
    active.cues.forEach((cue, i) => {
      if (cue.start_s == null || cue.start_s > scrubTimeS) return
      const diff = scrubTimeS - cue.start_s
      if (diff < bestDiff) {
        bestDiff = diff
        best = i
      }
    })
    return best >= 0 ? best : null
  }, [scrubTimeS, active])

  useEffect(() => {
    if (nearestIdx == null) return
    const el = cueRefs.current[nearestIdx]
    const container = containerRef.current
    if (!el || !container) return
    const target = el.offsetTop - container.clientHeight / 2 + el.clientHeight / 2
    container.scrollTo({ top: Math.max(0, target), behavior: 'smooth' })
  }, [nearestIdx])

  return (
    <div className="flex min-w-0 flex-col">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h4 className="font-medium text-slate-300">{title}</h4>
        {showSourceSelector && sources.length > 1 && (
          <select
            aria-label={`${title} source`}
            value={index}
            onChange={(event) => setIndex(Number(event.target.value))}
            className="rounded border border-slate-700 bg-slate-800 px-1 py-0.5 text-xs text-slate-300"
          >
            {sources.map((source, i) => (
              <option key={i} value={i}>
                {source.label}
              </option>
            ))}
          </select>
        )}
      </div>
      {active && active.cues.length > 0 ? (
        <div ref={containerRef} className="h-[400px] overflow-y-auto rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-400">
          {active.cues.map((cue, i) => (
            <p
              key={i}
              ref={(el) => {
                cueRefs.current[i] = el
              }}
              title={cue.start_s != null ? formatTimestampS(cue.start_s) : undefined}
              className={`whitespace-pre-wrap rounded px-1 py-0.5 ${
                nearestIdx === i ? 'bg-indigo-500/20 text-indigo-200' : ''
              }`}
            >
              {cue.text}
            </p>
          ))}
        </div>
      ) : (
        <p className="text-xs text-slate-500">{emptyText}</p>
      )}
    </div>
  )
}

export default TextPanel
