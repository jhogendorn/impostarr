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
 * file is. Four rows — header, ident, media, text — each explicitly
 * grid-row-placed so they align with RhsPanel's matching rows via the
 * shared subgrid the parent (ComparisonSection) sets up. The DB-reference
 * chips sit top-right of the header row, alongside the heading itself
 * (item 5) — no longer inline with the (variable-width) title, where they
 * used to wrap onto a second line. The media row holds Framegrabs, then
 * the timeline scrubber directly below it (item 4); the text row holds
 * the embedded-subs/transcript panels, aligned with RhsPanel's Reference
 * Subtitles panel one row down from Framegrabs. */
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
      <div className="row-start-1 flex items-center justify-between gap-2">
        <h3 className="font-medium text-slate-200">Sonarr {instanceName ? titleCase(instanceName) : 'Unknown'} Label</h3>
        <ExternalLinks ids={externalIds} />
      </div>
      <div className="row-start-2">
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
      </div>
      <div className="row-start-3 mt-2">
        <FramegrabStrip jobId={jobId} assets={assets} scrubTimeS={scrubTimeS} />
        <TimelineScrubber durationS={durationS} valueS={scrubTimeS ?? 0} onChange={onScrub} />
      </div>
      <div className="row-start-4 mt-3 grid grid-cols-2 gap-4">
        <TextPanel
          title="Embedded Subtitles"
          sources={embeddedSubsSources}
          emptyText="No embedded subtitles extracted."
          scrubTimeS={scrubTimeS}
          showSourceSelector={false}
        />
        <TextPanel title="Transcript" sources={transcriptSources} emptyText="No transcript available." scrubTimeS={scrubTimeS} />
      </div>
    </>
  )
}

export default LhsPanel
