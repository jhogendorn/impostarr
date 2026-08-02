import { useEffect, useState, type ReactNode } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { assetUrl, getJob } from '../api/client'
import type { JobDetail, JobDetailVerdict, SeriesExternalIds } from '../api/types'
import { formatPercent, formatTimestampS, pathBasename } from '../lib/format'
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

/** Plain-language translation of a normalized candidate's `kind`, so the
 * plugin-results table never leaks raw in_series/cross_series/junk tokens.
 * `in_series` returns `null` (rendered nowhere) rather than an annotation —
 * the episode label (SxxEyy) is already shown by the candidate itself, so
 * "matches this series" would just be a leaked restatement of the internal
 * tag with no added information. Annotations are kept only where they
 * carry information the candidate line doesn't already show. */
function describeNormalized(entry: unknown): string | null {
  if (typeof entry !== 'object' || entry === null) return String(entry)
  const record = entry as Record<string, unknown>
  if (record.kind === 'in_series') return null
  if (record.kind === 'cross_series') return `different series: ${JSON.stringify(record.external_ids)}`
  if (record.kind === 'junk') return 'no match'
  if (typeof record.reason === 'string') return `could not map: ${record.reason}`
  return JSON.stringify(entry)
}

/** Used only by the "Labelled as"/"Identification" derivations below — the
 * plugin-results table above uses its own inline (unpadded) formatting,
 * unchanged from the restored version. */
function normalizedKind(value: unknown): NormalizedKind | 'unnormalizable' | 'unknown' {
  if (typeof value !== 'object' || value === null) return 'unknown'
  const record = value as Record<string, unknown>
  if (record.kind === 'in_series' || record.kind === 'cross_series' || record.kind === 'junk') return record.kind
  if (typeof record.reason === 'string') return 'unnormalizable'
  return 'unknown'
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

function frameTimestampS(toolMeta: unknown): number | null {
  if (typeof toolMeta !== 'object' || toolMeta === null) return null
  const value = (toolMeta as Record<string, unknown>).timestamp_s
  return typeof value === 'number' ? value : null
}

// -- external-id links --------------------------------------------------

/** Either the job-detail-level `external_ids` shape (tvdb_id/imdb_id/tmdb_id,
 * from Sonarr's Series model) or a normalized cross_series candidate's
 * (tvdb/imdb/tmdb, from the plugin contract's ExternalIds) — both are
 * rendered the same way. */
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

// -- "labelled as" season/episode derivation -----------------------------
//
// JobDetail.file only carries Sonarr episode ids, not season/episode
// numbers. Every applicable ('ok') plugin result is contractually
// guaranteed a candidate with ident.series === 'claimed' (see
// plugins/base.py's PluginResult validator) — that candidate's
// season/episodes is the human-readable numbering for the file's claimed
// identity, so it's reused here rather than adding a second Sonarr episode
// lookup to the backend just for display.
function claimedSeasonEpisode(detail: JobDetail): CandidateIdent | null {
  for (const pr of detail.plugin_results) {
    if (pr.status !== 'ok' || !Array.isArray(pr.candidates)) continue
    for (const raw of pr.candidates) {
      if (isCandidate(raw) && raw.ident && raw.ident.series === 'claimed') return raw.ident
    }
  }
  return null
}

function seasonEpisodeLabel(ident: { season: number; episodes: number[] }): string {
  const season = String(ident.season).padStart(2, '0')
  return `S${season}${ident.episodes.map((ep) => `E${String(ep).padStart(2, '0')}`).join('')}`
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
function identificationStatement(
  detail: JobDetail,
  identified: IdentifiedAs,
  labelledSeasonEpisode: CandidateIdent | null,
): ReactNode {
  const verdict = detail.verdict
  if (!verdict) return 'Not yet processed.'
  // Checked before the s_claimed-null guard below: a human `is_other`
  // verdict never carries a numeric confidence (a human isn't scored), so
  // it would otherwise be misreported as "no usable evidence".
  if (identified.kind === 'human') {
    return `Manually identified as ${identified.seasonEpisode ? seasonEpisodeLabel(identified.seasonEpisode) : '—'} (human override).`
  }
  if (verdict.s_claimed === null) return 'Could not be identified — no usable evidence.'

  const labelText = labelledSeasonEpisode ? seasonEpisodeLabel(labelledSeasonEpisode) : 'the labelled episode'

  switch (identified.kind) {
    case 'remap': {
      const altText = identified.seasonEpisode ? seasonEpisodeLabel(identified.seasonEpisode) : 'a different episode'
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
 * saying the same thing twice (e.g. "Verified match." right under "Verified
 * as S01E01 — 92% confidence." is pure noise; "Fix applied." under a
 * mislabel statement is new information — an action was actually taken). */
function shouldShowOutcomeLine(identified: IdentifiedAs, verdict: JobDetailVerdict): boolean {
  if (verdict.outcome === 'remediate') return true
  if (verdict.outcome === 'matched') return false
  if (verdict.outcome === 'inconclusive') return false
  if (verdict.outcome === 'quarantine') {
    // The uncertain/matches-but-unconfident statement already ends in
    // "— needs review", so "Waiting for human review." is pure repetition.
    return identified.kind !== 'uncertain' && identified.kind !== 'matches'
  }
  return true
}

/** Fetches job detail on open; renders the labelled mapping, a single
 * identification statement, a fingerprint/dupe summary, the restored
 * per-plugin results table, transcript excerpt, framegrab strip (with
 * timestamp badges), probe summary, remediation log, and the VerdictActions
 * footer. */
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

  const frameAssets = detail?.assets.filter((asset) => asset.type === 'frames' && asset.has_path) ?? []
  const transcriptAsset = detail?.assets.find((asset) => asset.type === 'transcript')
  const probeAsset = detail?.assets.find((asset) => asset.type === 'probe')
  const transcript =
    transcriptAsset && isTranscriptPayload(transcriptAsset.payload) ? transcriptAsset.payload : undefined
  const probe = probeAsset && isProbePayload(probeAsset.payload) ? probeAsset.payload : undefined
  const remediationLogRaw = detail?.verdict?.remediation_log
  const remediationEntries: unknown[] = Array.isArray(remediationLogRaw) ? remediationLogRaw : []

  const labelledSeasonEpisode = detail ? claimedSeasonEpisode(detail) : null
  const identified = detail ? identifiedAs(detail) : null

  return (
    <Dialog open={open} onClose={onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="max-h-[85vh] w-full max-w-3xl overflow-y-auto rounded-lg border border-slate-700 bg-slate-900 p-6 text-slate-100">
          <div className="flex items-start justify-between gap-4">
            <DialogTitle className="text-lg font-semibold text-indigo-400">
              {detail ? (detail.external_ids?.title ?? `Series ${detail.file.series_id}`) : `Job #${jobId}`}
              {detail && <ExternalLinks ids={detail.external_ids} />}
            </DialogTitle>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            >
              ✕
            </button>
          </div>

          {loading && <p className="mt-4 text-sm text-slate-400">Loading…</p>}
          {error && <p className="mt-4 text-sm text-red-400">{error}</p>}

          {detail && identified && (
            <div className="mt-4 space-y-6 text-sm">
              <section>
                <h3 className="mb-1 font-medium text-slate-300">Labelled as</h3>
                <p className="text-slate-400">
                  {labelledSeasonEpisode
                    ? seasonEpisodeLabel(labelledSeasonEpisode)
                    : `episode(s) ${detail.file.episode_ids.join(', ')}`}{' '}
                  · {detail.instance ?? 'unknown instance'}
                </p>
                <p className="break-all text-slate-500">{detail.file.sonarr_path}</p>
              </section>

              <section>
                <h3 className="mb-1 font-medium text-slate-300">Identification</h3>
                <p className="text-slate-400">{identificationStatement(detail, identified, labelledSeasonEpisode)}</p>
                {detail.verdict && shouldShowOutcomeLine(identified, detail.verdict) && (
                  <p className="text-slate-400">{outcomeSentence(detail.verdict, detail.job.status, dryRun)}</p>
                )}
              </section>

              {(detail.frame_hash || detail.verdict?.dupe_info) && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-300">Fingerprint</h3>
                  {detail.frame_hash && (
                    <p className="text-slate-400">
                      Perceptual hash: {detail.frame_hash.n_frames} frames ({detail.frame_hash.algo} v
                      {detail.frame_hash.version})
                      {detail.phash_corpus && (
                        <>
                          {' '}
                          · stored in corpus ({detail.phash_corpus.source}, {formatPercent(detail.phash_corpus.confidence)})
                        </>
                      )}
                    </p>
                  )}
                  {detail.verdict?.dupe_info && (
                    <p className="text-amber-400">
                      Visually near-identical to{' '}
                      {detail.verdict.dupe_info.sonarr_path
                        ? pathBasename(detail.verdict.dupe_info.sonarr_path)
                        : 'another file'}{' '}
                      (similarity {formatPercent(detail.verdict.dupe_info.similarity)})
                    </p>
                  )}
                </section>
              )}

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
                    {detail.plugin_results.map((result) => (
                      <tr key={`${result.name}-${result.version}`} className="align-top text-slate-300">
                        <td className="py-1 pr-2">
                          {result.name} v{result.version}
                        </td>
                        <td className="py-1 pr-2">{result.status}</td>
                        <td className="py-1 pr-2 text-slate-500">{result.reason ?? '—'}</td>
                        <td className="py-1 pr-2">
                          {Array.isArray(result.candidates) && result.candidates.length > 0 ? (
                            <ul className="space-y-0.5">
                              {result.candidates.map((candidate, i) =>
                                isCandidate(candidate) ? (
                                  <li
                                    key={i}
                                    title={candidate.evidence ? JSON.stringify(candidate.evidence) : undefined}
                                  >
                                    conf {formatPercent(candidate.confidence)} · {candidate.numbering ?? '—'}{' '}
                                    {candidate.ident
                                      ? `S${candidate.ident.season}E${candidate.ident.episodes.join(',')}`
                                      : ''}
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
                              ? result.normalized.map(describeNormalized).filter((text) => text !== null)
                              : []
                            return (
                              annotations.length > 0 && (
                                <ul className="mt-1 space-y-0.5 text-slate-500">
                                  {annotations.map((text, i) => (
                                    <li key={i}>{text}</li>
                                  ))}
                                </ul>
                              )
                            )
                          })()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              {transcript?.segments && transcript.segments.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-300">Transcript excerpt</h3>
                  <pre className="max-h-48 overflow-y-auto rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-400">
                    {transcript.segments
                      .slice(0, 15)
                      .map((segment) => `[${segment.start.toFixed(1)}] ${segment.text}`)
                      .join('\n')}
                  </pre>
                </section>
              )}

              {frameAssets.length > 0 && (
                <section>
                  <h3 className="mb-1 font-medium text-slate-300">Framegrabs</h3>
                  <div className="flex flex-wrap gap-2">
                    {frameAssets.map((asset) => {
                      const timestampS = frameTimestampS(asset.tool_meta)
                      return (
                        <div key={asset.id} className="relative">
                          <img
                            src={assetUrl(detail.job.id, asset.id)}
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
                </section>
              )}

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

              <VerdictActions job={detail} onChanged={handleChanged} />
            </div>
          )}
        </DialogPanel>
      </div>
    </Dialog>
  )
}

export default InspectModal
