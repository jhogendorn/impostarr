import type { Asset, SeriesExternalIds } from '../api/types'
import { titleCase } from '../lib/format'
import { ExternalLinks } from './ExternalLinks'
import FramegrabStrip from './FramegrabStrip'
import TextPanel, { type TextPanelSource } from './TextPanel'

interface LhsPanelProps {
  instanceName: string | null
  labelText: string
  titleText: string | null
  externalIds: SeriesExternalIds | null
  jobId: number
  assets: Asset[]
  embeddedSubsSources: TextPanelSource[]
  transcriptSources: TextPanelSource[]
  scrubTimeS: number | null
}

/** LHS (2/3-width) column of the comparison section: what Sonarr says this
 * file is. Four rows — header, ident, links, content — each explicitly
 * grid-row-placed so they align with RhsPanel's matching rows via the
 * shared subgrid the parent (ComparisonSection) sets up. */
function LhsPanel({
  instanceName,
  labelText,
  titleText,
  externalIds,
  jobId,
  assets,
  embeddedSubsSources,
  transcriptSources,
  scrubTimeS,
}: LhsPanelProps) {
  return (
    <>
      <div className="row-start-1 text-sm font-medium text-slate-400">Sonarr {instanceName ? titleCase(instanceName) : 'Unknown'} Label</div>
      <div className="row-start-2">
        <p className="text-xl font-semibold text-slate-100">
          {labelText}
          {titleText ? ` - ${titleText}` : ''}
        </p>
      </div>
      <div className="row-start-3 -mt-1">
        <ExternalLinks ids={externalIds} />
      </div>
      <div className="row-start-4 mt-2">
        <FramegrabStrip jobId={jobId} assets={assets} scrubTimeS={scrubTimeS} />
        <div className="mt-3 grid grid-cols-2 gap-4">
          <TextPanel title="Embedded Subtitles" sources={embeddedSubsSources} emptyText="No embedded subtitles extracted." scrubTimeS={scrubTimeS} />
          <TextPanel title="Transcript" sources={transcriptSources} emptyText="No transcript available." scrubTimeS={scrubTimeS} />
        </div>
      </div>
    </>
  )
}

export default LhsPanel
