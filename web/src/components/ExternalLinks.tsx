import type { SeriesExternalIds } from '../api/types'

/** Either the job-detail-level `external_ids` shape (tvdb_id/imdb_id/
 * tmdb_id/sonarr_url, from Sonarr's Series model) or a normalized
 * cross_series candidate's (tvdb/imdb, from the plugin contract's
 * ExternalIds) — both rendered the same way. Numeric-string ids (e.g. an
 * LLM-sourced evidence blob that serialized a tvdb id as `"73141"` instead
 * of `73141`) are coerced rather than silently dropped — the previous
 * strict `typeof === 'number'`/`typeof === 'string'` checks rendered
 * nothing (dead, unlinked text) for any evidence shape that didn't match
 * exactly, which is the likeliest root cause of the "dead TVDB/IMDB text"
 * reported in the field: candidate evidence is free-form JSON from
 * third-party plugins/LLMs, not guaranteed to match Sonarr's own typing. */
export function tvdbUrl(ids: SeriesExternalIds | Record<string, unknown> | null | undefined): string | null {
  if (!ids) return null
  const record = ids as Record<string, unknown>
  const tvdb = record.tvdb_id ?? record.tvdb
  if (typeof tvdb === 'number') return `https://thetvdb.com/dereferrer/series/${tvdb}`
  if (typeof tvdb === 'string' && /^\d+$/.test(tvdb)) return `https://thetvdb.com/dereferrer/series/${tvdb}`
  return null
}

export function imdbUrl(ids: SeriesExternalIds | Record<string, unknown> | null | undefined): string | null {
  if (!ids) return null
  const record = ids as Record<string, unknown>
  const imdb = record.imdb_id ?? record.imdb
  if (typeof imdb === 'string' && imdb.length > 0) return `https://www.imdb.com/title/${imdb}/`
  return null
}

export function ExternalLinks({ ids }: { ids: SeriesExternalIds | Record<string, unknown> | null | undefined }) {
  const tvdb = tvdbUrl(ids)
  const imdb = imdbUrl(ids)
  if (!tvdb && !imdb) return null
  return (
    <span className="ml-2 space-x-2 text-xs">
      {tvdb && (
        <a href={tvdb} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">
          TVDB
        </a>
      )}
      {imdb && (
        <a href={imdb} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">
          IMDB
        </a>
      )}
    </span>
  )
}
