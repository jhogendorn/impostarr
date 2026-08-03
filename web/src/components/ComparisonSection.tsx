import { useMemo, useState } from 'react'
import type { JobDetail } from '../api/types'
import {
  type AlternateCandidate,
  isProbePayload,
  isSubsPayload,
  isTranscriptPayload,
  labelledEpisodes,
  labelledTitles,
} from '../lib/inspectData'
import { TWO_COLUMN_GRID_CLASS } from '../lib/layout'
import ConfidenceBadge from './ConfidenceBadge'
import type { TextPanelSource } from './TextPanel'
import LhsPanel from './LhsPanel'
import RhsPanel from './RhsPanel'

/** Section C: the two-column comparison (LHS 2/3 "Sonarr says", RHS 1/3
 * "Content Identity") and the out-of-flow confidence badge straddling
 * their boundary. The timeline scrubber lives inside LhsPanel now (item
 * 4, directly below Framegrabs) rather than spanning above both columns.
 *
 * LHS and RHS are each a single CSS-grid child using `grid-template-rows:
 * subgrid` against this component's own 4 explicit row tracks (header,
 * ident, media, text — the links row folds into header as top-right DB
 * chips, item 5/v5) — that's what keeps header/ident/media/text
 * row-aligned between the two columns: RHS's "Reference Subtitles" panel
 * (row 4) lines up with LHS's Embedded Subtitles/Transcript row, not with
 * Framegrabs (row 3), since RHS renders nothing into row 3 and subgrid
 * still gives it that row's real height. The confidence badge sits in a
 * zero-width middle grid column (the visual seam between the two boxes)
 * and is itself `position: absolute` within it — out of flow, centered
 * exactly on the boundary, row-placed to line up with the ident row.
 *
 * `selectedEpisodeId`/`candidates` are owned by InspectModal (shared with
 * ActionBar's Apply Remap combobox, item 1) — this component only reads
 * them to drive RhsPanel's preview and the badge's confidence value. */
function ComparisonSection({
  detail,
  selectedEpisodeId,
  candidates,
}: {
  detail: JobDetail
  selectedEpisodeId: number
  candidates: AlternateCandidate[]
}) {
  const labelledIds = useMemo(() => new Set(detail.file.episode_ids), [detail])

  const selectedCandidate = candidates.find((c) => c.episodeIds.includes(selectedEpisodeId))
  const confidence = selectedCandidate
    ? selectedCandidate.confidence
    : labelledIds.has(selectedEpisodeId)
      ? (detail.verdict?.s_claimed ?? null)
      : null

  const [scrubTimeS, setScrubTimeS] = useState<number | null>(null)

  const probeAsset = detail.assets.find((asset) => asset.type === 'probe')
  const probe = probeAsset && isProbePayload(probeAsset.payload) ? probeAsset.payload : undefined
  const durationS = probe?.format?.duration ? Number(probe.format.duration) : 0

  const embeddedSubsSources: TextPanelSource[] = detail.assets
    .filter((asset) => asset.type === 'subs')
    .map((asset) => {
      const payload = isSubsPayload(asset.payload) ? asset.payload : null
      const cues = payload?.cues ?? []
      return { label: payload?.language ?? `subtitle track ${asset.id}`, cues }
    })
    .filter((source) => source.cues.length > 0)

  const transcriptAsset = detail.assets.find((asset) => asset.type === 'transcript')
  const transcriptPayload = transcriptAsset && isTranscriptPayload(transcriptAsset.payload) ? transcriptAsset.payload : null
  const transcriptSources: TextPanelSource[] = transcriptPayload?.segments?.length
    ? [
        {
          label: transcriptPayload.language ?? 'transcript',
          cues: transcriptPayload.segments.map((seg) => ({ start_s: seg.start, text: seg.text })),
        },
      ]
    : []

  const referenceTrack = detail.reference_subtitles.find((track) => (track.episode_ids ?? []).includes(selectedEpisodeId))
  const referenceSources: TextPanelSource[] = referenceTrack
    ? [{ label: referenceTrack.language ? `${referenceTrack.label} (${referenceTrack.language})` : referenceTrack.label, cues: referenceTrack.cues }]
    : []

  return (
    <section className={`relative ${TWO_COLUMN_GRID_CLASS} grid-rows-[auto_auto_auto_auto] gap-y-3`}>
      <div className="glow-panel row-span-4 grid grid-rows-subgrid rounded-lg p-4" style={{ gridRow: '1 / span 4' }}>
        <LhsPanel
          instanceName={detail.instance}
          episodeIds={detail.file.episode_ids}
          labelledEpisodes={labelledEpisodes(detail)}
          titleText={labelledTitles(detail)}
          externalIds={detail.external_ids}
          jobId={detail.job.id}
          assets={detail.assets}
          embeddedSubsSources={embeddedSubsSources}
          transcriptSources={transcriptSources}
          durationS={durationS}
          scrubTimeS={scrubTimeS}
          onScrub={setScrubTimeS}
        />
      </div>

      <div className="relative" style={{ gridRow: 2 }}>
        <ConfidenceBadge confidence={confidence} />
      </div>

      <div className="glow-panel row-span-4 grid grid-rows-subgrid rounded-lg p-4" style={{ gridRow: '1 / span 4' }}>
        <RhsPanel detail={detail} selectedEpisodeId={selectedEpisodeId} referenceSources={referenceSources} scrubTimeS={scrubTimeS} />
      </div>
    </section>
  )
}

export default ComparisonSection
