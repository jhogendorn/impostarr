import { tvdbEpisodeUrl } from './ExternalLinks'

export interface EpisodeRefEpisode {
  episode: number
  /** Sonarr's per-episode TVDB id, when known/resolved — `undefined`/`null`
   * renders this episode's "Eyy" segment as plain text instead of a link. */
  tvdbId?: number | null
}

/** Renders "SxxEyy" (zero-padded, multi-episode as "SxxEyyEzz" — same
 * shape as `formatSeasonEpisode`), except each individual "Eyy" segment is
 * its own link to that episode's TVDB page when a tvdb id is known for it.
 * "Sxx" itself is never a link (a season has no single TVDB page).
 *
 * This is the one place every rendered episode reference in the inspect
 * panel (plugin-table candidates, LHS ident, RHS ident, proposal text)
 * should go through — see inspect-v4 spec item A: the "tvdb" text
 * elsewhere in the panel is `candidate.numbering` (a numbering SCHEME
 * label), not a database reference, and stays plain text. */
function EpisodeRef({ season, episodes }: { season: number; episodes: EpisodeRefEpisode[] }) {
  const s = String(season).padStart(2, '0')
  return (
    <>
      {`S${s}`}
      {episodes.map((ep, i) => {
        const text = `E${String(ep.episode).padStart(2, '0')}`
        const url = tvdbEpisodeUrl(ep.tvdbId)
        return url ? (
          <a
            key={i}
            href={url}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-dotted decoration-slate-500 underline-offset-2 hover:decoration-indigo-400 hover:text-indigo-300"
          >
            {text}
          </a>
        ) : (
          <span key={i}>{text}</span>
        )
      })}
    </>
  )
}

export default EpisodeRef
