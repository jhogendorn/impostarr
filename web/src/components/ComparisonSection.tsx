import { useMemo, useState } from 'react'
import type { JobDetail } from '../api/types'
import {
  collectAlternates,
  isProbePayload,
  isSubsPayload,
  isTranscriptPayload,
  labelledText,
  labelledTitles,
} from '../lib/inspectData'
import ConfidenceBadge from './ConfidenceBadge'
import type { TextPanelSource } from './TextPanel'
import LhsPanel from './LhsPanel'
import RhsPanel from './RhsPanel'
import TimelineScrubber from './TimelineScrubber'

/** Section C: the two-column comparison (LHS 2/3 "Sonarr says", RHS 1/3
 * "Content Identity"), a shared timeline scrubber above both, and the
 * out-of-flow confidence badge straddling their boundary.
 *
 * LHS and RHS are each a single CSS-grid child using `grid-template-rows:
 * subgrid` against this component's own 4 explicit row tracks — that's
 * what keeps header/ident/links/content row-aligned between the two
 * columns (a plain two-column flex layout can't guarantee that: either
 * column's content can be taller per-row than the other's). The
 * confidence badge sits in a zero-width middle grid column (the visual
 * seam between the two boxes) and is itself `position: absolute` within
 * it — out of flow, centered exactly on the boundary, row-placed to
 * line up with the ident row. */
function ComparisonSection({ detail }: { detail: JobDetail }) {
  const labelledIds = useMemo(() => new Set(detail.file.episode_ids), [detail])
  const candidates = useMemo(() => collectAlternates(detail, labelledIds), [detail, labelledIds])
  const allEpisodes = useMemo(
    () => [...(detail.series_episodes ?? [])].sort((a, b) => a.season - b.season || a.episode - b.episode),
    [detail],
  )

  const defaultEpisodeId = candidates.length > 0 ? candidates[0].episodeIds[0] : detail.file.episode_ids[0]
  const [selectedEpisodeId, setSelectedEpisodeId] = useState(defaultEpisodeId)

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
    <section>
      <TimelineScrubber durationS={durationS} valueS={scrubTimeS ?? 0} onChange={setScrubTimeS} />
      <div className="relative grid grid-cols-[2fr_0_1fr] grid-rows-[auto_auto_auto_auto] gap-x-16 gap-y-3">
        <div className="glow-panel row-span-4 grid grid-rows-subgrid rounded-lg p-4" style={{ gridRow: '1 / span 4' }}>
          <LhsPanel
            instanceName={detail.instance}
            labelText={labelledText(detail)}
            titleText={labelledTitles(detail)}
            externalIds={detail.external_ids}
            jobId={detail.job.id}
            assets={detail.assets}
            embeddedSubsSources={embeddedSubsSources}
            transcriptSources={transcriptSources}
            scrubTimeS={scrubTimeS}
          />
        </div>

        <div className="relative" style={{ gridRow: 2 }}>
          <ConfidenceBadge confidence={confidence} />
        </div>

        <div className="glow-panel row-span-4 grid grid-rows-subgrid rounded-lg p-4" style={{ gridRow: '1 / span 4' }}>
          <RhsPanel
            detail={detail}
            selectedEpisodeId={selectedEpisodeId}
            onSelectEpisode={setSelectedEpisodeId}
            candidates={candidates}
            allEpisodes={allEpisodes}
            referenceSources={referenceSources}
            scrubTimeS={scrubTimeS}
          />
        </div>
      </div>
    </section>
  )
}

export default ComparisonSection
