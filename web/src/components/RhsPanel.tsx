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
 * combobox never disagree about what's being previewed.
 *
 * DB chips sit top-right of the header row (item 5), same as LhsPanel.
 * Row 3 (LhsPanel's Framegrabs/scrubber row) is left empty here —
 * subgrid still gives it that row's real height, which is what lines
 * Reference Subtitles (row 4) up with Embedded Subtitles/Transcript
 * rather than with Framegrabs (item 4). */
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
      <div className="row-start-1 flex items-center justify-between gap-2">
        <h3 className="font-medium text-slate-200">Content Identity</h3>
        <ExternalLinks ids={detail.external_ids} />
      </div>
      <div className="row-start-2">
        <p className="text-xl font-semibold text-slate-100">
          {selectedLabel ? (
            <EpisodeRef season={selectedLabel.season} episodes={[{ episode: selectedLabel.episode, tvdbId: selectedLabel.tvdb_id }]} />
          ) : (
            '—'
          )}
          {selectedLabel?.title ? ` - ${selectedLabel.title}` : ''}
        </p>
        {matchesClaimedLabel && <span className="text-xs text-emerald-400">Matches Sonarr label</span>}
      </div>
      <div className="row-start-4 mt-3">
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
