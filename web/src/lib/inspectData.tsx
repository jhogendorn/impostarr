/** Shared pure logic + small presentational helpers for the inspect panel
 * (v3): runtime shape guards for the API's genuinely-`unknown` JSON fields,
 * episode-label resolution, external-id link building, and the
 * candidate/alternate-selection logic shared by the action bar's "Apply
 * Remap" dropdown and the comparison section's "Content Identity"
 * selector. Extracted from the pre-v3 monolithic InspectModal.tsx so the
 * v3 section components (ActionBar, ComparisonSection, PluginResultsSection)
 * can all import the same logic instead of re-deriving it. */
import type { ReactNode } from 'react'
import type { EpisodeLabel, JobDetail, TimedCue } from '../api/types'
import { ExternalLinks } from '../components/ExternalLinks'
import { formatSeasonEpisode } from './format'

export interface CandidateIdent {
  series: unknown
  season: number
  episodes: number[]
}

export interface Candidate {
  confidence: number
  ident: CandidateIdent | null
  numbering: string | null
  evidence?: Record<string, unknown>
}

export interface RemediationStep {
  step: string
  ok: boolean
  detail: unknown
  ts: string
}

export interface ProbePayload {
  format?: { duration?: string; format_name?: string }
  streams?: unknown[]
}

export interface TranscriptSegment {
  start: number
  end: number
  text: string
}

export interface TranscriptPayload {
  segments?: TranscriptSegment[]
  language?: string
}

export interface SubsPayload {
  cues?: TimedCue[]
  language?: string | null
}

export type NormalizedKind = 'in_series' | 'cross_series' | 'junk'

export interface NormalizedInSeries {
  kind: 'in_series'
  episode_ids: number[]
}

export interface NormalizedCrossSeries {
  kind: 'cross_series'
  external_ids: Record<string, unknown>
}

// -- runtime shape guards -------------------------------------------------

export function isCandidateIdent(value: unknown): value is CandidateIdent {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.season === 'number' && Array.isArray(record.episodes)
}

export function isCandidate(value: unknown): value is Candidate {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (typeof record.confidence !== 'number') return false
  if (typeof record.evidence !== 'object' || record.evidence === null) return false
  if (record.numbering != null && typeof record.numbering !== 'string') return false
  if (record.ident != null && !isCandidateIdent(record.ident)) return false
  return true
}

export function normalizedKind(value: unknown): NormalizedKind | 'unnormalizable' | 'unknown' {
  if (typeof value !== 'object' || value === null) return 'unknown'
  const record = value as Record<string, unknown>
  if (record.kind === 'in_series' || record.kind === 'cross_series' || record.kind === 'junk') return record.kind
  if (typeof record.reason === 'string') return 'unnormalizable'
  return 'unknown'
}

/** Plain-language annotation for a normalized candidate, for the
 * plugin-results table. `in_series` returns `null` (rendered nowhere) —
 * the episode label is already shown by the candidate itself. `cross_series`
 * renders real TVDB/IMDB links from that candidate's OWN external_ids. */
export function normalizedAnnotation(entry: unknown): ReactNode | null {
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

export function isRemediationStep(value: unknown): value is RemediationStep {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.step === 'string' && typeof record.ok === 'boolean' && typeof record.ts === 'string'
}

export function isProbePayload(value: unknown): value is ProbePayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.format != null && (typeof record.format !== 'object' || Array.isArray(record.format))) return false
  if (record.streams != null && !Array.isArray(record.streams)) return false
  return true
}

export function isTranscriptSegment(value: unknown): value is TranscriptSegment {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return typeof record.start === 'number' && typeof record.end === 'number' && typeof record.text === 'string'
}

export function isTranscriptPayload(value: unknown): value is TranscriptPayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.segments != null && !(Array.isArray(record.segments) && record.segments.every(isTranscriptSegment))) {
    return false
  }
  return true
}

export function isTimedCue(value: unknown): value is TimedCue {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return (record.start_s === null || typeof record.start_s === 'number') && typeof record.text === 'string'
}

export function isSubsPayload(value: unknown): value is SubsPayload {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  if (record.cues != null && !(Array.isArray(record.cues) && record.cues.every(isTimedCue))) {
    return false
  }
  return true
}

export function frameTimestampS(toolMeta: unknown): number | null {
  if (typeof toolMeta !== 'object' || toolMeta === null) return null
  const value = (toolMeta as Record<string, unknown>).timestamp_s
  return typeof value === 'number' ? value : null
}


// -- episode-id -> label resolution ----------------------------------------

/** The file's own claimed episode(s), resolved to season/episode/title via
 * `detail.episode_labels` (built server-side from a live Sonarr episode
 * lookup — see routes.py `_episode_labels`). Falls back to a plain
 * "episode(s) N" string built from raw ids only if the lookup didn't
 * resolve every one of them. */
export function labelledEpisodes(detail: JobDetail): EpisodeLabel[] {
  return detail.file.episode_ids
    .map((id) => detail.episode_labels[String(id)])
    .filter((label): label is EpisodeLabel => label != null)
}

export function labelledText(detail: JobDetail): string {
  const labels = labelledEpisodes(detail)
  if (labels.length !== detail.file.episode_ids.length || labels.length === 0) {
    return `episode(s) ${detail.file.episode_ids.join(', ')}`
  }
  return formatSeasonEpisode(labels[0].season, labels.map((l) => l.episode))
}

export function labelledTitles(detail: JobDetail): string | null {
  const labels = labelledEpisodes(detail)
  const titles = labels.map((l) => l.title).filter((t): t is string => !!t)
  return titles.length > 0 ? titles.join(' / ') : null
}

export function episodeTitlesForIds(detail: JobDetail, ids: number[]): string | null {
  const titles = ids
    .map((id) => detail.episode_labels[String(id)]?.title)
    .filter((t): t is string => !!t)
  return titles.length > 0 ? titles.join(' / ') : null
}

/** "S01E01 - Title" for a resolved episode label, or bare "S01E01" when
 * the title isn't known (never a dangling " - "). Used by every episode
 * picker in the panel (Apply Remap dropdown, Content Identity selector). */
export function episodeOptionLabel(label: EpisodeLabel): string {
  const se = formatSeasonEpisode(label.season, [label.episode])
  return label.title ? `${se} - ${label.title}` : se
}

// -- candidate / alternate collection --------------------------------------

export interface AlternateCandidate {
  episodeIds: number[]
  season: number
  episodes: number[]
  confidence: number
}

/** Every distinct in-series alternate candidate across all plugins,
 * excluding `excludeIds` (the labelled episode) — deduped by episode-id
 * set, keeping the highest confidence seen for each, sorted descending.
 * Backs both the action bar's "Apply Remap" dropdown Candidates group and
 * the comparison section's "Content Identity" selector Candidates group. */
export function collectAlternates(detail: JobDetail, excludeIds: Set<number>): AlternateCandidate[] {
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

export function findCrossSeriesCandidate(detail: JobDetail): Record<string, unknown> | null {
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

export function pluginReasoning(candidates: unknown): string | null {
  if (!Array.isArray(candidates)) return null
  const reasons = candidates
    .map((c) => (isCandidate(c) ? c.evidence?.reasoning : undefined))
    .filter((r): r is string => typeof r === 'string' && r.length > 0)
  return reasons.length > 0 ? [...new Set(reasons)].join(' / ') : null
}

// -- proposed-action presence (drives section A + Apply Remap defaults) ---

export interface ProposedRemapAction {
  kind: 'remap'
  target_episode_ids: number[]
}

export interface ProposedReplaceAction {
  kind: 'replace'
}

/** Whether a proposal (auto-computed action or a human `is_other`
 * ident) currently exists — gates section A (Proposed Action banner). */
export function hasProposal(detail: JobDetail): boolean {
  const verdict = detail.verdict
  if (!verdict) return false
  return verdict.proposed_action != null || (verdict.source === 'human' && verdict.human_ident != null)
}

export function proposalTone(detail: JobDetail): 'remap' | 'replace' | null {
  const action = detail.verdict?.proposed_action
  if (action && typeof action === 'object') {
    const kind = (action as Record<string, unknown>).kind
    if (kind === 'remap') return 'remap'
    if (kind === 'replace') return 'replace'
  }
  if (detail.verdict?.source === 'human' && detail.verdict.human_ident) return 'remap'
  return null
}

/** Resolves proposed-action target episode ids to "S05E03" via
 * `job.episode_labels`. Falls back to a plain-language "episode(s) 684"
 * only if the resolution is incomplete. */
export function targetLabel(job: JobDetail, targetIds: number[]): string {
  const resolved = targetIds.map((id) => job.episode_labels[String(id)]).filter((label) => label != null)
  if (targetIds.length === 0 || resolved.length !== targetIds.length) {
    return targetIds.length > 0 ? `episode(s) ${targetIds.join(', ')}` : 'the proposed episode'
  }
  return formatSeasonEpisode(resolved[0].season, resolved.map((r) => r.episode))
}

/** Text describing the not-yet-approved proposal, from either an
 * auto-computed `proposed_action` or a human `is_other` verdict's
 * `human_ident`. Backs the Proposed Action banner (section A). */
export function describeProposal(job: JobDetail): string | null {
  const verdict = job.verdict
  if (!verdict) return null
  const action = verdict.proposed_action
  if (action !== null && typeof action === 'object') {
    const record = action as Record<string, unknown>
    if (record.kind === 'remap') {
      const ids = record.target_episode_ids
      return `Remap to ${Array.isArray(ids) ? targetLabel(job, ids as number[]) : 'unknown episode'}`
    }
    if (record.kind === 'replace') {
      return 'Replace — no known episode of this series matched this file'
    }
  }
  if (verdict.source === 'human' && verdict.human_ident) {
    const { season, episodes } = verdict.human_ident
    return `Remap to ${formatSeasonEpisode(season, episodes)}`
  }
  return null
}
