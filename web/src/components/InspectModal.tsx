import { useEffect, useState, type ReactNode } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { assetUrl, datapackUrl, getJob } from '../api/client'
import type { Asset, EpisodeLabel, JobDetail, JobDetailVerdict, SeriesExternalIds } from '../api/types'
import { formatPercent, formatSeasonEpisode, formatTimestampS, pathBasename } from '../lib/format'
import VerdictActions from './VerdictActions'

interface InspectModalProps {
  jobId: number | null
  open: boolean
  onClose: () => void
  onChanged: () => void
  dryRun?: boolean
}

interface CandidateIdent {
  series: unknown
  season: number
  episodes: number[]
}

interface Candidate {
  confidence: number
  ident: CandidateIdent | null
  numbering: string | null
  evidence?: Record<string, unknown>
}

interface RemediationStep {
  step: string
  ok: boolean
  detail: unknown
  ts: string
}

interface ProbePayload {
  format?: { duration?: string; format_name?: string }
  streams?: unknown[]
}

interface TranscriptSegment {
  start: number
  end: number
  text: string
}

interface TranscriptPayload {
  segments?: TranscriptSegment[]
  language?: string
}

interface SubsPayload {
  cues?: string[]
  language?: string | null
}

type NormalizedKind = 'in_series' | 'cross_series' | 'junk'

interface NormalizedInSeries {
  kind: 'in_series'
  episode_ids: number[]
}

interface NormalizedCrossSeries {
  kind: 'cross_series'
  external_ids: Record<string, unknown>
}

// -- runtime shape guards for the API's genuinely-`unknown` JSON fields
// (candidates/normalized/remediation_log/asset payload/tool_meta) — a
// malformed entry renders a visible fallback instead of throwing mid-render.

function isCandidateIdent(value: unknown): value is CandidateIdent {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.season === 'number' && Array.isArray(record.episodes)
}

function isCandidate(value: unknown): value is Candidate {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (typeof record.confidence !== 'number') return false
  if (typeof record.evidence !== 'object' || record.evidence === null) return false
  if (record.numbering != null && typeof record.numbering !== 'string') return false
  if (record.ident != null && !isCandidateIdent(record.ident)) return false
  return true
}

function normalizedKind(value: unknown): NormalizedKind | 'unnormalizable' | 'unknown' {
  if (typeof value !== 'object' || value === null) return 'unknown'
  const record = value as Record<string, unknown>
  if (record.kind === 'in_series' || record.kind === 'cross_series' || record.kind === 'junk') return record.kind
  if (typeof record.reason === 'string') return 'unnormalizable'
  return 'unknown'
}

/** Plain-language annotation for a normalized candidate, for the
 * plugin-results table. `in_series` returns `null` (rendered nowhere) —
 * the episode label (SxxEyy) is already shown by the candidate itself, so
 * restating "matches this series" adds nothing. `cross_series` renders
 * real TVDB/IMDB links from that candidate's OWN external_ids (which may
 * point at a different series than the header's) instead of a raw
 * JSON dump — see UI-SPEC section 3. A true per-*episode* deep link isn't
 * constructible (Sonarr's Episode model here has no per-episode TVDB/IMDB
 * id), so these link to the referenced series' page. */
function normalizedAnnotation(entry: unknown): ReactNode | null {
  if (typeof entry !== 'object' || entry === null) return <>{String(entry)}</>
  const record = entry as Record<string, unknown>
  if (record.kind === 'in_series') return null
  if (record.kind === 'cross_series') {
    return (
      <>
        different series
        <ExternalLinks ids={record.external_ids as Record<string, unknown>} />
      </>
    )
  }
  if (record.kind === 'junk') return <>no match</>
  if (typeof record.reason === 'string') return <>could not map: {record.reason}</>
  return <>{JSON.stringify(entry)}</>
}

function isRemediationStep(value: unknown): value is RemediationStep {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.step === 'string' && typeof record.ok === 'boolean' && typeof record.ts === 'string'
}

function isProbePayload(value: unknown): value is ProbePayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.format != null && (typeof record.format !== 'object' || Array.isArray(record.format))) return false
  if (record.streams != null && !Array.isArray(record.streams)) return false
  return true
}

function isTranscriptSegment(value: unknown): value is TranscriptSegment {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.start === 'number' && typeof record.end === 'number' && typeof record.text === 'string'
}

function isTranscriptPayload(value: unknown): value is TranscriptPayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.segments != null && !(Array.isArray(record.segments) && record.segments.every(isTranscriptSegment))) {
    return false
  }
  return true
}

function isSubsPayload(value: unknown): value is SubsPayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.cues != null && !(Array.isArray(record.cues) && record.cues.every((c) => typeof c === 'string'))) {
    return false
  }
  return true
}

function frameTimestampS(toolMeta: unknown): number | null {
  if (typeof toolMeta !== 'object' || toolMeta === null) return null
  const value = (toolMeta as Record<string, unknown>).timestamp_s
  return typeof value === 'number' ? value : null
}

// -- external-id links --------------------------------------------------

/** Either the job-detail-level `external_ids` shape (tvdb_id/imdb_id/
 * tmdb_id/sonarr_url, from Sonarr's Series model) or a normalized
 * cross_series candidate's (tvdb/imdb, from the plugin contract's
 * ExternalIds) — both are rendered the same way. */
function tvdbUrl(ids: SeriesExternalIds | Record<string, unknown> | null | undefined): string | null {
  if (!ids) return null
  const record = ids as Record<string, unknown>
  const tvdb = record.tvdb_id ?? record.tvdb
  return typeof tvdb === 'number' ? `https://thetvdb.com/dereferrer/series/${tvdb}` : null
}

function imdbUrl(ids: SeriesExternalIds | Record<string, unknown> | null | undefined): string | null {
  if (!ids) return null
  const record = ids as Record<string, unknown>
  const imdb = record.imdb_id ?? record.imdb
  return typeof imdb === 'string' ? `https://www.imdb.com/title/${imdb}/` : null
}

function ExternalLinks({ ids }: { ids: SeriesExternalIds | Record<string, unknown> | null | undefined }) {
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

// -- episode-id -> label resolution (P0.5) -------------------------------

/** The file's own claimed episode(s), resolved to season/episode/title via
 * `detail.episode_labels` (built server-side from a live Sonarr episode
 * lookup — see routes.py `_episode_labels`). Falls back to a plain
 * "episode(s) N" string built from raw ids only if the lookup didn't
 * resolve every one of them (e.g. the instance was unreachable when this
 * job detail was fetched) — never renders a bare id when a label exists. */
function labelledEpisodes(detail: JobDetail): EpisodeLabel[] {
  return detail.file.episode_ids
    .map((id) => detail.episode_labels[String(id)])
    .filter((label): label is EpisodeLabel => label != null)
}

function labelledText(detail: JobDetail): string {
  const labels = labelledEpisodes(detail)
  if (labels.length !== detail.file.episode_ids.length || labels.length === 0) {
    return `episode(s) ${detail.file.episode_ids.join(', ')}`
  }
  return formatSeasonEpisode(labels[0].season, labels.map((l) => l.episode))
}

function labelledTitles(detail: JobDetail): string | null {
  const labels = labelledEpisodes(detail)
  const titles = labels.map((l) => l.title).filter((t): t is string => !!t)
  return titles.length > 0 ? titles.join(' / ') : null
}

function episodeTitlesForIds(detail: JobDetail, ids: number[]): string | null {
  const titles = ids
    .map((id) => detail.episode_labels[String(id)]?.title)
    .filter((t): t is string => !!t)
  return titles.length > 0 ? titles.join(' / ') : null
}

// -- single identification statement --------------------------------------
//
// Tied to the server's own routing decision (`verdict.proposed_action`)
// rather than re-deriving scoring from scratch client-side: `remap` names
// the exact winning in-series alternate by episode id, `replace` means the
// winner was cross-series/junk (that distinction isn't itself persisted, so
// it's inferred from whichever plugin candidate best explains it).

interface IdentifiedAs {
  kind: 'matches' | 'remap' | 'cross_series' | 'junk' | 'human' | 'uncertain' | 'unknown'
  seasonEpisode?: CandidateIdent
  externalIds?: Record<string, unknown>
  confidence?: number | null
}

function findInSeriesLabel(detail: JobDetail, targetIds: Set<number>): CandidateIdent | null {
  for (const pr of detail.plugin_results) {
    if (!Array.isArray(pr.candidates) || !Array.isArray(pr.normalized)) continue
    for (let i = 0; i < pr.candidates.length; i++) {
      const norm = pr.normalized[i]
      if (normalizedKind(norm) !== 'in_series') continue
      const ids = (norm as NormalizedInSeries).episode_ids
      if (Array.isArray(ids) && ids.length === targetIds.size && ids.every((id) => targetIds.has(id))) {
        const cand = pr.candidates[i]
        if (isCandidate(cand) && cand.ident) return cand.ident
      }
    }
  }
  return null
}

function findCrossSeriesCandidate(detail: JobDetail): Record<string, unknown> | null {
  for (const pr of detail.plugin_results) {
    if (!Array.isArray(pr.candidates) || !Array.isArray(pr.normalized)) continue
    for (let i = 0; i < pr.candidates.length; i++) {
      if (normalizedKind(pr.normalized[i]) === 'cross_series') {
        return (pr.normalized[i] as NormalizedCrossSeries).external_ids
      }
    }
  }
  return null
}

/** Every distinct in-series alternate candidate across all plugins,
 * excluding `excludeIds` (the labelled episode) — deduped by episode-id
 * set, keeping the highest confidence seen for each, sorted descending.
 * Backs the "Identified as" column's dropdown selector (UI-SPEC section
 * 2) when more than one credible alternate exists. There's no per-file
 * "alt threshold" exposed by the job-detail API (scoring.py's
 * Thresholds.alt is instance-config, not per-job data) — showing every
 * distinct in-series alternate and letting the dropdown appear once
 * there's more than one is the pragmatic reading of "above alt
 * threshold" without inventing a client-side copy of server config. */
interface AlternateCandidate {
  episodeIds: number[]
  season: number
  episodes: number[]
  confidence: number
}

function collectAlternates(detail: JobDetail, excludeIds: Set<number>): AlternateCandidate[] {
  const byKey = new Map<string, AlternateCandidate>()
  for (const pr of detail.plugin_results) {
    if (pr.status !== 'ok' || !Array.isArray(pr.candidates) || !Array.isArray(pr.normalized)) continue
    for (let i = 0; i < pr.candidates.length; i++) {
      if (normalizedKind(pr.normalized[i]) !== 'in_series') continue
      const ids = (pr.normalized[i] as NormalizedInSeries).episode_ids
      if (!Array.isArray(ids) || ids.length === 0) continue
      if (ids.length === excludeIds.size && ids.every((id) => excludeIds.has(id))) continue
      const cand = pr.candidates[i]
      if (!isCandidate(cand) || !cand.ident) continue
      const key = [...ids].sort((a, b) => a - b).join(',')
      const existing = byKey.get(key)
      if (!existing || cand.confidence > existing.confidence) {
        byKey.set(key, { episodeIds: ids, season: cand.ident.season, episodes: cand.ident.episodes, confidence: cand.confidence })
      }
    }
  }
  return [...byKey.values()].sort((a, b) => b.confidence - a.confidence)
}

function identifiedAs(detail: JobDetail): IdentifiedAs {
  const verdict = detail.verdict
  if (!verdict) return { kind: 'unknown' }
  if (verdict.source === 'human' && verdict.human_ident) {
    return { kind: 'human', seasonEpisode: { season: verdict.human_ident.season, episodes: verdict.human_ident.episodes, series: 'claimed' } }
  }
  if (verdict.s_claimed === null) return { kind: 'unknown' }
  if (verdict.outcome === 'matched') return { kind: 'matches' }

  const action = verdict.proposed_action
  if (action && action.kind === 'remap' && Array.isArray(action.target_episode_ids)) {
    const targetIds = new Set(action.target_episode_ids as number[])
    const label = findInSeriesLabel(detail, targetIds)
    return { kind: 'remap', seasonEpisode: label ?? undefined, confidence: verdict.s_alt }
  }
  if (action && action.kind === 'replace') {
    const cross = findCrossSeriesCandidate(detail)
    if (cross) return { kind: 'cross_series', externalIds: cross, confidence: verdict.s_alt }
    return { kind: 'junk', confidence: verdict.s_alt }
  }
  if (verdict.s_alt !== null) return { kind: 'uncertain', confidence: verdict.s_alt }
  return { kind: 'matches' }
}

/** ONE sentence covering both "what was identified" and "how confident" —
 * replaces the old separate Identified-as/Confidence sections, which read
 * as two disconnected facts about the same result. */
function identificationStatement(detail: JobDetail, identified: IdentifiedAs): ReactNode {
  const verdict = detail.verdict
  if (!verdict) return 'Not yet processed.'
  // Checked before the s_claimed-null guard below: a human `is_other`
  // verdict never carries a numeric confidence (a human isn't scored), so
  // it would otherwise be misreported as "no usable evidence".
  if (identified.kind === 'human') {
    return `Manually identified as ${identified.seasonEpisode ? formatSeasonEpisode(identified.seasonEpisode.season, identified.seasonEpisode.episodes) : '—'} (human override).`
  }
  if (verdict.s_claimed === null) return 'Could not be identified — no usable evidence.'

  const labelText = labelledText(detail)

  switch (identified.kind) {
    case 'remap': {
      const altText = identified.seasonEpisode
        ? formatSeasonEpisode(identified.seasonEpisode.season, identified.seasonEpisode.episodes)
        : 'a different episode'
      return `This file appears to be ${altText} (${formatPercent(identified.confidence ?? null)} confidence), not the labelled ${labelText} (${formatPercent(verdict.s_claimed)}).`
    }
    case 'cross_series':
      return (
        <>
          This file appears to be a different series entirely ({formatPercent(identified.confidence ?? null)} confidence).
          <ExternalLinks ids={identified.externalIds} />
        </>
      )
    case 'junk':
      return `This file's content didn't match any known episode (${formatPercent(identified.confidence ?? null)} confidence).`
    case 'matches':
    case 'uncertain':
      if (verdict.outcome === 'matched') {
        return `Verified as ${labelText} — ${formatPercent(verdict.s_claimed)} confidence.`
      }
      return `Uncertain match to the labelled episode (${formatPercent(verdict.s_claimed)} confidence) — needs review.`
    default:
      return 'Could not be identified.'
  }
}

// -- plain-language outcome sentence --------------------------------------

function outcomeSentence(verdict: JobDetailVerdict | null, jobStatus: string, dryRun: boolean): string {
  if (!verdict) return 'Not yet processed.'
  switch (verdict.outcome) {
    case 'matched':
      return 'Verified match.'
    case 'quarantine':
      return 'Waiting for human review.'
    case 'inconclusive':
      return 'Not enough evidence to judge.'
    case 'remediate':
      if (jobStatus === 'remediated') return dryRun ? 'Fix applied (dry run).' : 'Fix applied.'
      if (jobStatus === 'error') return 'Error while applying the fix.'
      return 'Fix attempted, needs review.'
    default:
      return `${verdict.outcome[0].toUpperCase()}${verdict.outcome.slice(1)}.`
  }
}

/** Whether the plain-language outcome line adds anything beyond the single
 * identification statement above it — shown only when it does, to avoid
 * saying the same thing twice. */
function shouldShowOutcomeLine(identified: IdentifiedAs, verdict: JobDetailVerdict): boolean {
  if (verdict.outcome === 'remediate') return true
  if (verdict.outcome === 'matched') return false
  if (verdict.outcome === 'inconclusive') return false
  if (verdict.outcome === 'quarantine') {
    return identified.kind !== 'uncertain' && identified.kind !== 'matches'
  }
  return true
}

// -- identity header (UI-SPEC section 1: filename + series + season/ep +
// instance, always present at top) ----------------------------------------

function IdentityHeader({ detail, jobId, onClose }: { detail: JobDetail | null; jobId: number | null; onClose: () => void }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <DialogTitle className="break-all text-lg font-semibold text-indigo-400">
          {detail ? pathBasename(detail.file.sonarr_path) : `Job #${jobId}`}
        </DialogTitle>
        {detail && (
          <p className="mt-1 text-sm text-slate-400">
            {detail.external_ids?.title ?? `Series ${detail.file.series_id}`}
            <ExternalLinks ids={detail.external_ids} />
            {' · '}
            {labelledText(detail)}
            {' · '}
            {detail.instance ?? 'unknown instance'}
          </p>
        )}
      </div>
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="shrink-0 rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
      >
        ✕
      </button>
    </div>
  )
}

// -- three-way text comparison (UI-SPEC section 2) -------------------------

interface TextSource {
  label: string
  text: string
}

/** One independently-scrollable text panel with a source selector shown
 * only when more than one source exists (e.g. multiple embedded-subtitle
 * languages, or multiple reference-subtitle episodes compared). */
function TextPanel({ title, sources, emptyText }: { title: string; sources: TextSource[]; emptyText: string }) {
  const [index, setIndex] = useState(0)
  const active = sources[Math.min(index, sources.length - 1)]
  return (
    <div className="flex min-w-0 flex-col">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h4 className="font-medium text-slate-300">{title}</h4>
        {sources.length > 1 && (
          <select
            aria-label={`${title} source`}
            value={index}
            onChange={(event) => setIndex(Number(event.target.value))}
            className="rounded border border-slate-700 bg-slate-800 px-1 py-0.5 text-xs text-slate-300"
          >
            {sources.map((source, i) => (
              <option key={i} value={i}>
                {source.label}
              </option>
            ))}
          </select>
        )}
      </div>
      {active ? (
        <pre className="h-40 overflow-y-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-400">
          {active.text}
        </pre>
      ) : (
        <p className="text-xs text-slate-500">{emptyText}</p>
      )}
    </div>
  )
}

// -- framegrab strip (rendered inside the "As labelled" column — see
// UI-SPEC section 2: identified-side visuals are deferred) ----------------

function FramegrabStrip({ jobId, assets }: { jobId: number; assets: Asset[] }) {
  const frameAssets = assets.filter((asset) => asset.type === 'frames' && asset.has_path)
  if (frameAssets.length === 0) return null
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {frameAssets.map((asset) => {
        const timestampS = frameTimestampS(asset.tool_meta)
        return (
          <div key={asset.id} className="relative">
            <img
              src={assetUrl(jobId, asset.id)}
              loading="lazy"
              alt={`frame ${asset.id}`}
              className="h-20 w-auto rounded border border-slate-700"
            />
            {timestampS !== null && (
              <span className="absolute bottom-0.5 right-0.5 rounded bg-black/70 px-1 text-[10px] text-slate-100">
                {formatTimestampS(timestampS)}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// -- comparison section (UI-SPEC section 2) --------------------------------

function ComparisonSection({ detail, identified }: { detail: JobDetail; identified: IdentifiedAs }) {
  const labelledIds = new Set(detail.file.episode_ids)
  const alternates = collectAlternates(detail, labelledIds)
  const [selectedAlt, setSelectedAlt] = useState(0)
  const active = alternates[Math.min(selectedAlt, Math.max(alternates.length - 1, 0))]

  const centerConfidence =
    identified.kind === 'matches' || identified.kind === 'unknown'
      ? detail.verdict?.s_claimed ?? null
      : (identified.confidence ?? detail.verdict?.s_claimed ?? null)

  const embeddedSubsSources: TextSource[] = detail.assets
    .filter((asset) => asset.type === 'subs')
    .map((asset) => {
      const payload = isSubsPayload(asset.payload) ? asset.payload : null
      const cues = payload?.cues ?? []
      return { label: payload?.language ?? `subtitle track ${asset.id}`, text: cues.join('\n') }
    })
    .filter((source) => source.text.length > 0)

  const transcriptAsset = detail.assets.find((asset) => asset.type === 'transcript')
  const transcriptPayload = transcriptAsset && isTranscriptPayload(transcriptAsset.payload) ? transcriptAsset.payload : null
  const transcriptSources: TextSource[] = transcriptPayload?.segments?.length
    ? [
        {
          label: transcriptPayload.language ?? 'transcript',
          text: transcriptPayload.segments.map((seg) => `[${seg.start.toFixed(1)}] ${seg.text}`).join('\n'),
        },
      ]
    : []

  const referenceSources: TextSource[] = detail.reference_subtitles.map((track) => ({
    label: track.language ? `${track.label} (${track.language})` : track.label,
    text: track.cues.join('\n'),
  }))

  return (
    <section>
      {/* A single grid spanning both rows keeps the identity row and the
       * three-way text row in the same 3 columns (left / center / right)
       * without a second nested grid. */}
      <div className="grid grid-cols-[1fr_auto_1fr] gap-x-4 gap-y-4">
        <div className="min-w-0">
          <h3 className="mb-1 font-medium text-slate-300">As labelled</h3>
          <p className="text-slate-200">{labelledText(detail)}</p>
          {labelledTitles(detail) && <p className="text-slate-400">{labelledTitles(detail)}</p>}
          {/* Identified-side visuals (e.g. a reference still) are
           * deferred — online stills sourcing is an open roadmap
           * question, see UI-SPEC section 2. Only the file's own
           * framegrabs are shown, here on the labelled side. */}
          <FramegrabStrip jobId={detail.job.id} assets={detail.assets} />
        </div>

        <div className="flex flex-col items-center justify-center px-2 text-center">
          <span className="text-2xl font-semibold text-slate-100">{formatPercent(centerConfidence)}</span>
          <span className="text-xs uppercase tracking-wide text-slate-500">confidence</span>
        </div>

        <div className="min-w-0">
          <h3 className="mb-1 font-medium text-slate-300">Identified as</h3>
          {identified.kind === 'human' && identified.seasonEpisode && (
            <>
              <p className="text-slate-200">{formatSeasonEpisode(identified.seasonEpisode.season, identified.seasonEpisode.episodes)}</p>
              <p className="text-xs text-slate-500">human override</p>
            </>
          )}
          {(identified.kind === 'matches' || identified.kind === 'uncertain') && alternates.length === 0 && (
            <p className="text-slate-200">{labelledText(detail)} (same as labelled)</p>
          )}
          {identified.kind === 'cross_series' && (
            <p className="text-slate-200">
              Different series
              <ExternalLinks ids={identified.externalIds} />
            </p>
          )}
          {identified.kind === 'junk' && <p className="text-slate-500">No known episode matched</p>}
          {(identified.kind === 'remap' || ((identified.kind === 'uncertain' || identified.kind === 'matches') && alternates.length > 0)) && (
            <>
              {alternates.length > 1 && (
                <select
                  aria-label="Alternate identification"
                  value={selectedAlt}
                  onChange={(event) => setSelectedAlt(Number(event.target.value))}
                  className="mb-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-200"
                >
                  {alternates.map((alt, i) => (
                    <option key={i} value={i}>
                      {formatSeasonEpisode(alt.season, alt.episodes)} ({formatPercent(alt.confidence)})
                    </option>
                  ))}
                </select>
              )}
              {active ? (
                <>
                  <p className="text-slate-200">{formatSeasonEpisode(active.season, active.episodes)}</p>
                  {episodeTitlesForIds(detail, active.episodeIds) && (
                    <p className="text-slate-400">{episodeTitlesForIds(detail, active.episodeIds)}</p>
                  )}
                </>
              ) : (
                identified.seasonEpisode && (
                  <p className="text-slate-200">{formatSeasonEpisode(identified.seasonEpisode.season, identified.seasonEpisode.episodes)}</p>
                )
              )}
            </>
          )}
          {identified.kind === 'unknown' && <p className="text-slate-500">Not yet identified</p>}
        </div>

        <TextPanel title="Embedded subtitles" sources={embeddedSubsSources} emptyText="No embedded subtitles extracted." />
        <TextPanel title="Transcript" sources={transcriptSources} emptyText="No transcript available." />
        <TextPanel title="Reference subtitles" sources={referenceSources} emptyText="No reference subtitles compared." />
      </div>
    </section>
  )
}

// -- plugin results table (UI-SPEC section 3) ------------------------------

/** Any LLM-plugin reasoning recorded on this row's candidates
 * (`evidence.reasoning`, already stored by subs-llm/transcript-llm — see
 * their plugin.py `Candidate(..., evidence={"reasoning": ...})`) — shown
 * as a hover tooltip on the plugin name rather than a always-visible
 * column, since only LLM-backed plugins populate it. */
function pluginReasoning(candidates: unknown): string | null {
  if (!Array.isArray(candidates)) return null
  const reasons = candidates
    .map((c) => (isCandidate(c) ? c.evidence?.reasoning : undefined))
    .filter((r): r is string => typeof r === 'string' && r.length > 0)
  return reasons.length > 0 ? [...new Set(reasons)].join(' / ') : null
}

function PluginResultsTable({ detail }: { detail: JobDetail }) {
  return (
    <section>
      <h3 className="mb-1 font-medium text-slate-300">Plugin results</h3>
      <table className="w-full text-left text-xs">
        <thead className="text-slate-500">
          <tr>
            <th className="py-1 pr-2">Plugin</th>
            <th className="py-1 pr-2">Status</th>
            <th className="py-1 pr-2">Reason</th>
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
                <td className="py-1 pr-2">{result.status}</td>
                <td className="py-1 pr-2 text-slate-500">{result.reason ?? '—'}</td>
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

// -- phash section (UI-SPEC section 4 — moved above probe summary) --------

function PhashSection({ detail }: { detail: JobDetail }) {
  if (!detail.frame_hash && !detail.verdict?.dupe_info) return null
  const otherFileUrl = detail.external_ids?.sonarr_url
  return (
    <section>
      <h3 className="mb-1 font-medium text-slate-300">Fingerprint</h3>
      {detail.frame_hash && (
        <p className="text-slate-400">
          Perceptual hash: {detail.frame_hash.n_frames} frames sampled, algo {detail.frame_hash.algo} v
          {detail.frame_hash.version}
          {detail.phash_corpus && (
            <> · in corpus ({detail.phash_corpus.source}, {formatPercent(detail.phash_corpus.confidence)} confidence)</>
          )}
        </p>
      )}
      {detail.verdict?.dupe_info && (
        <p className="text-amber-400">
          Visually near-identical to{' '}
          {/* No per-file deep link is constructible (there's no
           * job-lookup-by-file-id endpoint) — this links to the series'
           * Sonarr page as the closest available reference, same fallback
           * as the plugin-results table (see routes.py
           * `_series_external_ids`). */}
          {otherFileUrl ? (
            <a href={otherFileUrl} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">
              {detail.verdict.dupe_info.sonarr_path ? pathBasename(detail.verdict.dupe_info.sonarr_path) : 'another file'}
            </a>
          ) : detail.verdict.dupe_info.sonarr_path ? (
            pathBasename(detail.verdict.dupe_info.sonarr_path)
          ) : (
            'another file'
          )}{' '}
          (similarity {formatPercent(detail.verdict.dupe_info.similarity)})
        </p>
      )}
    </section>
  )
}

// -- debug datapack download (UI-SPEC section 7) ---------------------------

function DatapackDownload({ jobId }: { jobId: number }) {
  const [includePaths, setIncludePaths] = useState(false)
  return (
    <section className="border-t border-slate-800 pt-4 text-xs text-slate-500">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={includePaths}
          onChange={(event) => setIncludePaths(event.target.checked)}
          className="h-3.5 w-3.5 accent-indigo-500"
        />
        Include file paths — the datapack always includes full file paths; this just confirms you want a copy that does
      </label>
      <div className="mt-2">
        {includePaths ? (
          <a
            href={datapackUrl(jobId)}
            download
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
          >
            Download debug datapack
          </a>
        ) : (
          <button
            type="button"
            disabled
            className="rounded-lg border border-slate-800 px-3 py-1.5 text-slate-600 disabled:cursor-not-allowed"
          >
            Download debug datapack
          </button>
        )}
      </div>
    </section>
  )
}

/** Fetches job detail on open; renders the identity header (always
 * present, sticky along with the action bar), the two-column
 * comparison + three-way text comparison, the plugin-results table, the
 * fingerprint/phash section, probe summary, remediation log, and the
 * debug-datapack download. */
function InspectModal({ jobId, open, onClose, onChanged, dryRun = false }: InspectModalProps) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || jobId === null) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetail(null)
    setLoading(true)
    setError(null)
    getJob(jobId)
      .then((data) => {
        if (!cancelled) setDetail(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, jobId])

  function handleChanged() {
    onChanged()
    if (jobId !== null) {
      getJob(jobId)
        .then(setDetail)
        .catch(() => {})
    }
  }

  const probeAsset = detail?.assets.find((asset) => asset.type === 'probe')
  const probe = probeAsset && isProbePayload(probeAsset.payload) ? probeAsset.payload : undefined
  const remediationLogRaw = detail?.verdict?.remediation_log
  const remediationEntries: unknown[] = Array.isArray(remediationLogRaw) ? remediationLogRaw : []

  const identified = detail ? identifiedAs(detail) : null

  return (
    <Dialog open={open} onClose={onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="glow-elevated max-h-[85vh] w-full max-w-3xl overflow-y-auto rounded-lg bg-slate-900 p-6 text-slate-100">
          {/* Identity (section 1) + action bar (section 6) share this
           * sticky header — negative margins let it bleed to the panel's
           * edges while the panel keeps its own padding for the rest. */}
          <div className="sticky top-0 z-10 -mx-6 -mt-6 border-b border-slate-800 bg-slate-900 px-6 pb-4 pt-6">
            <IdentityHeader detail={detail} jobId={jobId} onClose={onClose} />
            {detail && (
              <div className="mt-3">
                <VerdictActions job={detail} onChanged={handleChanged} />
              </div>
            )}
          </div>

          {loading && <p className="mt-4 text-sm text-slate-400">Loading…</p>}
          {error && <p className="mt-4 text-sm text-red-400">{error}</p>}

          {detail && identified && (
            <div className="mt-6 space-y-6 text-sm">
              <section>
                <h3 className="mb-1 font-medium text-slate-300">Identification</h3>
                <p className="text-slate-400">{identificationStatement(detail, identified)}</p>
                {detail.verdict && shouldShowOutcomeLine(identified, detail.verdict) && (
                  <p className="text-slate-400">{outcomeSentence(detail.verdict, detail.job.status, dryRun)}</p>
                )}
              </section>

              <ComparisonSection detail={detail} identified={identified} />
              <PluginResultsTable detail={detail} />
              <PhashSection detail={detail} />

              {probe?.format && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-300">Probe summary</h3>
                  <p className="text-slate-400">
                    duration {probe.format.duration ?? '—'}s · container {probe.format.format_name ?? '—'} ·
                    streams {probe.streams?.length ?? 0}
                  </p>
                </section>
              )}

              {remediationEntries.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-300">Remediation log</h3>
                  <ol className="space-y-1">
                    {remediationEntries.map((entry, i) =>
                      isRemediationStep(entry) ? (
                        <li key={i} className={entry.ok ? 'text-slate-400' : 'text-red-400'}>
                          {entry.ts} · {entry.step} · {entry.ok ? 'ok' : 'failed'} —{' '}
                          {typeof entry.detail === 'string' ? entry.detail : JSON.stringify(entry.detail)}
                        </li>
                      ) : (
                        <li key={i} className="text-slate-600">
                          unrecognized entry
                        </li>
                      ),
                    )}
                  </ol>
                </section>
              )}

              <DatapackDownload jobId={detail.job.id} />
            </div>
          )}
        </DialogPanel>
      </div>
    </Dialog>
  )
}

export default InspectModal
