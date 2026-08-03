import type { JobDetail } from '../api/types'
import { formatPercent, formatSeasonEpisode } from '../lib/format'
import { isCandidate, normalizedAnnotation, pluginReasoning } from '../lib/inspectData'

/** Section D: own delineated section (own heading + card). The Reason
 * column is removed — a plugin's abstain/error reason now shows as a
 * hover tooltip on the Status cell instead of an always-visible column
 * (most rows are `ok` with nothing to say there). TVDB/IMDB references
 * (via `normalizedAnnotation`'s cross_series case) are real links, not
 * plain text — see `components/ExternalLinks.tsx`'s `tvdbUrl`/`imdbUrl` for the
 * defensive numeric-string coercion that was the likeliest cause of a
 * dead/unlinked reference in the field. */
function PluginResultsSection({ detail }: { detail: JobDetail }) {
  return (
    <section className="glow-panel rounded-lg p-4">
      <h3 className="mb-2 font-medium text-slate-200">Plugin Results</h3>
      <table className="w-full text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-2">Plugin</th>
            <th className="py-1 pr-2">Status</th>
            <th className="py-1 pr-2">Candidates</th>
          </tr>
        </thead>
        <tbody>
          {detail.plugin_results.map((result) => {
            const reasoning = pluginReasoning(result.candidates)
            return (
              <tr key={`${result.name}-${result.version}`} className="align-top text-slate-300">
                <td className="py-1 pr-2" title={reasoning ?? undefined}>
                  {result.name} v{result.version}
                  {reasoning && <span className="ml-1 text-indigo-400">ⓘ</span>}
                </td>
                <td className="py-1 pr-2" title={result.reason ?? undefined}>
                  {result.status}
                </td>
                <td className="py-1 pr-2">
                  {Array.isArray(result.candidates) && result.candidates.length > 0 ? (
                    <ul className="space-y-0.5">
                      {result.candidates.map((candidate, i) =>
                        isCandidate(candidate) ? (
                          <li key={i} title={candidate.evidence ? JSON.stringify(candidate.evidence) : undefined}>
                            conf {formatPercent(candidate.confidence)} · {candidate.numbering ?? '—'}{' '}
                            {candidate.ident ? formatSeasonEpisode(candidate.ident.season, candidate.ident.episodes) : ''}
                          </li>
                        ) : (
                          <li key={i} className="text-slate-600">
                            unrecognized entry
                          </li>
                        ),
                      )}
                    </ul>
                  ) : (
                    '—'
                  )}
                  {(() => {
                    const annotations = Array.isArray(result.normalized)
                      ? result.normalized.map((entry) => normalizedAnnotation(entry)).filter((node) => node !== null)
                      : []
                    return (
                      annotations.length > 0 && (
                        <ul className="mt-1 space-y-0.5 text-slate-500">
                          {annotations.map((node, i) => (
                            <li key={i}>{node}</li>
                          ))}
                        </ul>
                      )
                    )
                  })()}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}

export default PluginResultsSection
