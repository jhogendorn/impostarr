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

/** Per-*episode* TVDB deep link, from Sonarr's own per-episode `tvdbId`
 * (see `Episode.tvdb_id`, sonarr/types.py) — distinct from `tvdbUrl` above,
 * which links the whole series. Backs every rendered SxxEyy in the
 * inspect panel (see `EpisodeRef`), not the title-row DB chips. */
export function tvdbEpisodeUrl(tvdbId: number | null | undefined): string | null {
  return typeof tvdbId === 'number' ? `https://thetvdb.com/dereferrer/episode/${tvdbId}` : null
}

export function imdbUrl(ids: SeriesExternalIds | Record<string, unknown> | null | undefined): string | null {
  if (!ids) return null
  const record = ids as Record<string, unknown>
  const imdb = record.imdb_id ?? record.imdb
  if (typeof imdb === 'string' && imdb.length > 0) return `https://www.imdb.com/title/${imdb}/`
  return null
}

const CHIP_CLASS =
  'inline-flex min-h-6 items-center rounded border border-slate-600 bg-slate-800 px-1.5 py-0.5 align-baseline text-[10px] font-semibold uppercase tracking-wide text-indigo-300 hover:bg-slate-700 hover:text-indigo-200'

/** Small uppercase DB-reference chips (item 14) — baseline-aligned inline
 * with whatever title text precedes them (LhsPanel/RhsPanel render this
 * right after the episode title, not on a separate row), annotation-
 * styled but still real, clickable links with a real (~24px) tap target. */
export function ExternalLinks({ ids }: { ids: SeriesExternalIds | Record<string, unknown> | null | undefined }) {
  const tvdb = tvdbUrl(ids)
  const imdb = imdbUrl(ids)
  if (!tvdb && !imdb) return null
  return (
    <span className="inline-flex items-center gap-1">
      {tvdb && (
        <a href={tvdb} target="_blank" rel="noreferrer" className={CHIP_CLASS}>
          TVDB
        </a>
      )}
      {imdb && (
        <a href={imdb} target="_blank" rel="noreferrer" className={CHIP_CLASS}>
          IMDB
        </a>
      )}
    </span>
  )
}
