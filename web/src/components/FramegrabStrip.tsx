import { useEffect, useMemo, useRef } from 'react'
import { assetUrl } from '../api/client'
import type { Asset } from '../api/types'
import { formatTimestampS } from '../lib/format'
import { frameTimestampS } from '../lib/inspectData'

interface FramegrabStripProps {
  jobId: number
  assets: Asset[]
  /** Current scrubber position (seconds) — the frame nearest this time
   * gets a highlight ring and is scrolled into view within the strip. */
  scrubTimeS?: number | null
}

/** Horizontal-scroll strip of the file's own framegrabs, restored
 * "Framegrabs" label + a "N frames" count badge (scrollability is easy to
 * miss on a strip that fits on-screen at small counts, so the badge makes
 * it obvious there's more even before scrolling) and a right-edge fade
 * mask as a second scroll affordance. Frames highlight to the nearest
 * timeline-scrubber position. */
function FramegrabStrip({ jobId, assets, scrubTimeS = null }: FramegrabStripProps) {
  const frameAssets = assets.filter((asset) => asset.type === 'frames' && asset.has_path)
  const scrollRef = useRef<HTMLDivElement>(null)
  const frameRefs = useRef<(HTMLDivElement | null)[]>([])

  const nearestIdx = useMemo(() => {
    if (scrubTimeS == null || frameAssets.length === 0) return null
    let best = 0
    let bestDiff = Infinity
    frameAssets.forEach((asset, i) => {
      const ts = frameTimestampS(asset.tool_meta)
      if (ts === null) return
      const diff = Math.abs(ts - scrubTimeS)
      if (diff < bestDiff) {
        bestDiff = diff
        best = i
      }
    })
    return best
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrubTimeS, frameAssets.length])

  useEffect(() => {
    if (nearestIdx == null) return
    const el = frameRefs.current[nearestIdx]
    const container = scrollRef.current
    if (!el || !container) return
    const target = el.offsetLeft - container.clientWidth / 2 + el.clientWidth / 2
    container.scrollTo({ left: Math.max(0, target), behavior: 'smooth' })
  }, [nearestIdx])

  if (frameAssets.length === 0) return null

  return (
    <div className="mt-3">
      <div className="mb-1 flex items-center gap-2">
        <h4 className="font-medium text-slate-300">Framegrabs</h4>
        <span className="rounded-full border border-slate-700 bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
          {frameAssets.length} frames
        </span>
      </div>
      <div className="relative">
        <div ref={scrollRef} className="flex gap-2 overflow-x-auto pb-1">
          {frameAssets.map((asset, i) => {
            const timestampS = frameTimestampS(asset.tool_meta)
            const highlighted = nearestIdx === i
            return (
              <div
                key={asset.id}
                ref={(el) => {
                  frameRefs.current[i] = el
                }}
                className={`relative shrink-0 rounded ${highlighted ? 'ring-2 ring-indigo-400' : ''}`}
              >
                <img
                  src={assetUrl(jobId, asset.id)}
                  loading="lazy"
                  alt={`frame ${asset.id}`}
                  className="h-auto w-40 shrink-0 rounded border border-slate-700"
                />
                {timestampS !== null && (
                  <span className="absolute bottom-0.5 right-0.5 rounded bg-black/70 px-1 text-[10px] text-slate-100">
                    {formatTimestampS(timestampS)}
                  </span>
                )}
              </div>
            )
          })}
        </div>
        {/* Right-edge fade as a second "there's more" affordance beyond the count badge. */}
        <div className="pointer-events-none absolute right-0 top-0 h-full w-8 bg-gradient-to-l from-slate-900 to-transparent" />
      </div>
    </div>
  )
}

export default FramegrabStrip
