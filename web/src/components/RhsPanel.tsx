import type { JobDetail } from '../api/types'
import EpisodeRef from './EpisodeRef'
import { ExternalLinks } from './ExternalLinks'
import TextPanel, { type TextPanelSource } from './TextPanel'

interface RhsPanelProps {
  detail: JobDetail
  selectedEpisodeId: number
  referenceSources: TextPanelSource[]
  scrubTimeS: number | null
}

/** RHS (1/3-width) column of the comparison section: what the evidence
 * actually supports — "Content Identity". Purely a PREVIEW of whichever
 * episode the Action Bar's Apply Remap combobox currently has selected
 * (item 1) — there is no selector here anymore; `selectedEpisodeId` is
 * owned by InspectModal and shared with ActionBar, so this panel and the
 * combobox never disagree about what's being previewed. */
function RhsPanel({ detail, selectedEpisodeId, referenceSources, scrubTimeS }: RhsPanelProps) {
  const selectedLabel =
    detail.episode_labels[String(selectedEpisodeId)] ?? detail.series_episodes.find((ep) => ep.id === selectedEpisodeId)
  // Item 16: when the RHS is previewing the file's OWN claimed episode
  // (because it's the leading identification, s_claimed >= s_alt — see
  // `leadingEpisodeId`), say so plainly rather than leaving the ident row
  // looking identical to LhsPanel's with no explanation of why.
  const matchesClaimedLabel = detail.file.episode_ids.includes(selectedEpisodeId)

  return (
    <>
      <h3 className="row-start-1 font-medium text-slate-200">Content Identity</h3>
      <div className="row-start-2 flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
        <p className="text-xl font-semibold text-slate-100">
          {selectedLabel ? (
            <EpisodeRef season={selectedLabel.season} episodes={[{ episode: selectedLabel.episode, tvdbId: selectedLabel.tvdb_id }]} />
          ) : (
            '—'
          )}
          {selectedLabel?.title ? ` - ${selectedLabel.title}` : ''}
        </p>
        <ExternalLinks ids={detail.external_ids} />
        {matchesClaimedLabel && <span className="text-xs text-emerald-400">Matches Sonarr label</span>}
      </div>
      <div className="row-start-3 mt-2">
        <TextPanel
          title="Reference Subtitles"
          sources={referenceSources}
          emptyText="No reference subtitles cached for this episode."
          scrubTimeS={scrubTimeS}
        />
      </div>
    </>
  )
}

export default RhsPanel
