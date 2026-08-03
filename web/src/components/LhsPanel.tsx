import type { Asset, EpisodeLabel, SeriesExternalIds } from '../api/types'
import { titleCase } from '../lib/format'
import EpisodeRef from './EpisodeRef'
import { ExternalLinks } from './ExternalLinks'
import FramegrabStrip from './FramegrabStrip'
import TextPanel, { type TextPanelSource } from './TextPanel'
import TimelineScrubber from './TimelineScrubber'

interface LhsPanelProps {
  instanceName: string | null
  /** The file's own claimed episode ids (raw) alongside however many of
   * them resolved to a label — mirrors `labelledText`'s fallback: only
   * rendered as a linked ident when EVERY claimed id resolved, else a
   * plain "episode(s) 100, 101" (never a bare unlabelled id, P0.5). */
  episodeIds: number[]
  labelledEpisodes: EpisodeLabel[]
  titleText: string | null
  externalIds: SeriesExternalIds | null
  jobId: number
  assets: Asset[]
  embeddedSubsSources: TextPanelSource[]
  transcriptSources: TextPanelSource[]
  durationS: number
  scrubTimeS: number | null
  onScrub: (seconds: number) => void
}

/** LHS (2/3-width) column of the comparison section: what Sonarr says this
 * file is. Three rows — header, ident, content — each explicitly
 * grid-row-placed so they align with RhsPanel's matching rows via the
 * shared subgrid the parent (ComparisonSection) sets up. The content row
 * holds Framegrabs, then the timeline scrubber directly below it (item
 * 4), then the embedded-subs/transcript text panels. */
function LhsPanel({
  instanceName,
  labelledEpisodes,
  titleText,
  externalIds,
  jobId,
  assets,
  embeddedSubsSources,
  transcriptSources,
  durationS,
  scrubTimeS,
  onScrub,
}: LhsPanelProps) {
  return (
    <>
      <h3 className="row-start-1 font-medium text-slate-200">Sonarr {instanceName ? titleCase(instanceName) : 'Unknown'} Label</h3>
      <div className="row-start-2 flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
        <p className="text-xl font-semibold text-slate-100">
          {labelledEpisodes.length > 0 ? (
            <EpisodeRef
              season={labelledEpisodes[0].season}
              episodes={labelledEpisodes.map((l) => ({ episode: l.episode, tvdbId: l.tvdb_id }))}
            />
          ) : (
            'episode(s) unknown'
          )}
          {titleText ? ` - ${titleText}` : ''}
        </p>
        <ExternalLinks ids={externalIds} />
      </div>
      <div className="row-start-3 mt-2">
        <FramegrabStrip jobId={jobId} assets={assets} scrubTimeS={scrubTimeS} />
        <TimelineScrubber durationS={durationS} valueS={scrubTimeS ?? 0} onChange={onScrub} />
        <div className="mt-3 grid grid-cols-2 gap-4">
          <TextPanel
            title="Embedded Subtitles"
            sources={embeddedSubsSources}
            emptyText="No embedded subtitles extracted."
            scrubTimeS={scrubTimeS}
            showSourceSelector={false}
          />
          <TextPanel title="Transcript" sources={transcriptSources} emptyText="No transcript available." scrubTimeS={scrubTimeS} />
        </div>
      </div>
    </>
  )
}

export default LhsPanel
