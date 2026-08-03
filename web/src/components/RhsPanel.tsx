import type { EpisodeLabel, JobDetail } from '../api/types'
import { formatSeasonEpisode } from '../lib/format'
import { type AlternateCandidate, episodeOptionLabel } from '../lib/inspectData'
import { ExternalLinks } from './ExternalLinks'
import TextPanel, { type TextPanelSource } from './TextPanel'

interface RhsPanelProps {
  detail: JobDetail
  selectedEpisodeId: number
  onSelectEpisode: (id: number) => void
  candidates: AlternateCandidate[]
  allEpisodes: EpisodeLabel[]
  referenceSources: TextPanelSource[]
  scrubTimeS: number | null
}

/** RHS (1/3-width) column of the comparison section: what the evidence
 * actually supports — "Content Identity". The episode selector renders
 * INLINE with the ident row (not above/below it) so the ident↔ident row
 * stays height-matched with LhsPanel's ident row; selecting an episode
 * here drives the ident text, DB links, and which reference-subtitle
 * track renders — there is no separate reference-subtitle selector. */
function RhsPanel({ detail, selectedEpisodeId, onSelectEpisode, candidates, allEpisodes, referenceSources, scrubTimeS }: RhsPanelProps) {
  const selectedLabel = detail.episode_labels[String(selectedEpisodeId)] ?? allEpisodes.find((ep) => ep.id === selectedEpisodeId)

  return (
    <>
      <div className="row-start-1 text-sm font-medium text-slate-400">Content Identity</div>
      <div className="row-start-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="text-xl font-semibold text-slate-100">
          {selectedLabel ? formatSeasonEpisode(selectedLabel.season, [selectedLabel.episode]) : '—'}
          {selectedLabel?.title ? ` - ${selectedLabel.title}` : ''}
        </p>
        <select
          aria-label="Content identity episode"
          value={selectedEpisodeId}
          onChange={(event) => onSelectEpisode(Number(event.target.value))}
          className="rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 text-xs text-slate-300"
        >
          {candidates.length > 0 && (
            <optgroup label="Candidates">
              {candidates.map((c) => {
                const epId = c.episodeIds[0]
                const label = detail.episode_labels[String(epId)] ?? allEpisodes.find((ep) => ep.id === epId)
                return (
                  <option key={epId} value={epId}>
                    {label ? episodeOptionLabel(label) : `episode ${epId}`}
                  </option>
                )
              })}
            </optgroup>
          )}
          <optgroup label="All Episodes">
            {allEpisodes.map((ep) => (
              <option key={ep.id} value={ep.id}>
                {episodeOptionLabel(ep)}
              </option>
            ))}
          </optgroup>
        </select>
      </div>
      <div className="row-start-3 -mt-1">
        <ExternalLinks ids={detail.external_ids} />
      </div>
      <div className="row-start-4 mt-2">
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
