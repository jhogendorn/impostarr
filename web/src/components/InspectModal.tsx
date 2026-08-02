import { useEffect, useState } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { assetUrl, getJob } from '../api/client'
import type { JobDetail } from '../api/types'
import { formatScore } from '../lib/format'
import VerdictActions from './VerdictActions'

interface InspectModalProps {
  jobId: number | null
  open: boolean
  onClose: () => void
  onChanged: () => void
}

interface Candidate {
  confidence: number
  ident: { series: unknown; season: number; episodes: number[] } | null
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

// -- runtime shape guards for the API's genuinely-`unknown` JSON fields
// (candidates/normalized/remediation_log/asset payload) — a malformed
// entry renders a visible fallback instead of throwing mid-render.

function isCandidateIdent(value: unknown): value is { series: unknown; season: number; episodes: number[] } {
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

function describeNormalized(entry: unknown): string {
  if (typeof entry !== 'object' || entry === null) return String(entry)
  const record = entry as Record<string, unknown>
  if (record.kind === 'in_series') return `in_series: ${(record.episode_ids as number[]).join(', ')}`
  if (record.kind === 'cross_series') return `cross_series: ${JSON.stringify(record.external_ids)}`
  if (record.kind === 'junk') return 'junk'
  if (typeof record.reason === 'string') return `unnormalizable: ${record.reason}`
  return JSON.stringify(entry)
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

/** Fetches job detail on open; renders claimed mapping, scores, per-plugin
 * results, transcript excerpt, framegrab strip, probe summary, remediation
 * log, and the VerdictActions footer. */
function InspectModal({ jobId, open, onClose, onChanged }: InspectModalProps) {
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

  return (
    <Dialog open={open} onClose={onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="max-h-[85vh] w-full max-w-3xl overflow-y-auto rounded-lg border border-slate-700 bg-slate-900 p-6 text-slate-100">
          <div className="flex items-start justify-between gap-4">
            <DialogTitle className="text-lg font-semibold text-indigo-400">
              Job #{jobId} {detail ? `— ${detail.job.status}` : ''}
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

          {detail && (
            <div className="mt-4 space-y-6 text-sm">
              <section>
                <h3 className="mb-1 font-medium text-slate-300">Claimed mapping</h3>
                <p className="text-slate-400">
                  series {detail.file.series_id} · episodes {detail.file.episode_ids.join(', ')}
                </p>
                <p className="break-all text-slate-500">{detail.file.sonarr_path}</p>
              </section>

              <section>
                <h3 className="mb-1 font-medium text-slate-300">Scores</h3>
                <p className="text-slate-400">
                  s_claimed {formatScore(detail.verdict?.s_claimed ?? null)} · s_alt{' '}
                  {formatScore(detail.verdict?.s_alt ?? null)} · outcome {detail.verdict?.outcome ?? '—'}
                </p>
              </section>

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
                                  <li key={i}>
                                    conf {candidate.confidence.toFixed(2)} · {candidate.numbering ?? '—'}{' '}
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
                          {Array.isArray(result.normalized) && result.normalized.length > 0 && (
                            <ul className="mt-1 space-y-0.5 text-slate-500">
                              {result.normalized.map((entry, i) => (
                                <li key={i}>{describeNormalized(entry)}</li>
                              ))}
                            </ul>
                          )}
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
                    {frameAssets.map((asset) => (
                      <img
                        key={asset.id}
                        src={assetUrl(detail.job.id, asset.id)}
                        loading="lazy"
                        alt={`frame ${asset.id}`}
                        className="h-20 w-auto rounded border border-slate-700"
                      />
                    ))}
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
