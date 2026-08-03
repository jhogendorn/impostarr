import type { JobDetail } from '../api/types'
import { formatPercent, pathBasename } from '../lib/format'
import { isProbePayload, isRemediationStep } from '../lib/inspectData'

/** Section E: fingerprint + probe summary together under one neutral
 * "Details" heading (previously two separate sections), plus the
 * remediation-log audit trail (not itself called out in the v3 spec's
 * section list, but nothing there calls for deleting it either — folded
 * in here as the natural home for secondary/technical job data rather
 * than dropping an audit trail as an unstated side effect of the
 * redesign). Renders whatever data actually exists as compact lines — no
 * placeholder/empty sub-headings when a given piece (corpus membership, a
 * dupe hit) isn't present; omits the whole section only when there's
 * truly nothing to show. */
function DetailsSection({ detail }: { detail: JobDetail }) {
  const probeAsset = detail.assets.find((asset) => asset.type === 'probe')
  const probe = probeAsset && isProbePayload(probeAsset.payload) ? probeAsset.payload : undefined
  const dupe = detail.verdict?.dupe_info
  const otherFileUrl = detail.external_ids?.sonarr_url
  const remediationLogRaw = detail.verdict?.remediation_log
  const remediationEntries: unknown[] = Array.isArray(remediationLogRaw) ? remediationLogRaw : []

  if (!detail.frame_hash && !dupe && !probe?.format && remediationEntries.length === 0) return null

  return (
    <section className="glow-panel rounded-lg p-4">
      <h3 className="mb-2 font-medium text-slate-200">Details</h3>
      <div className="space-y-1 text-sm text-slate-400">
        {detail.frame_hash && (
          <p>
            Perceptual hash: {detail.frame_hash.n_frames} frames sampled, algo {detail.frame_hash.algo} v
            {detail.frame_hash.version}
            {detail.phash_corpus && (
              <> · in corpus ({detail.phash_corpus.source}, {formatPercent(detail.phash_corpus.confidence)} confidence)</>
            )}
          </p>
        )}
        {dupe && (
          <p className="text-amber-400">
            Visually near-identical to{' '}
            {otherFileUrl ? (
              <a href={otherFileUrl} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">
                {dupe.sonarr_path ? pathBasename(dupe.sonarr_path) : 'another file'}
              </a>
            ) : dupe.sonarr_path ? (
              pathBasename(dupe.sonarr_path)
            ) : (
              'another file'
            )}{' '}
            (similarity {formatPercent(dupe.similarity)})
          </p>
        )}
        {probe?.format && (
          <p>
            duration {probe.format.duration ?? '—'}s · container {probe.format.format_name ?? '—'} · streams{' '}
            {probe.streams?.length ?? 0}
          </p>
        )}
      </div>
      {remediationEntries.length > 0 && (
        <div className="mt-3">
          <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Remediation Log</h4>
          <ol className="space-y-1 text-xs">
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
        </div>
      )}
    </section>
  )
}

export default DetailsSection
