import type { ReactNode } from 'react'
import type { JobDetail } from '../api/types'
import { formatPercent, pathBasename } from '../lib/format'
import { isProbePayload, isRemediationStep } from '../lib/inspectData'

function SubHeading({ children }: { children: ReactNode }) {
  return <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">{children}</h4>
}

/** Section E: "Details", with Fingerprint / Probe / Remediation Log as
 * labelled subsections (item 8 — previously a single merged blob with no
 * subheadings save the log). Renders whatever data actually exists as
 * compact lines per subsection — no placeholder/empty sub-headings when a
 * given piece (corpus membership, a dupe hit) isn't present; omits the
 * whole section only when there's truly nothing to show. */
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
      <div className="space-y-3 text-sm text-slate-400">
        {(detail.frame_hash || dupe) && (
          <div>
            <SubHeading>Fingerprint</SubHeading>
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
          </div>
        )}
        {probe?.format && (
          <div>
            <SubHeading>Probe</SubHeading>
            <p>
              duration {probe.format.duration ?? '—'}s · container {probe.format.format_name ?? '—'} · streams{' '}
              {probe.streams?.length ?? 0}
            </p>
          </div>
        )}
      </div>
      {remediationEntries.length > 0 && (
        <div className="mt-3">
          <SubHeading>Remediation Log</SubHeading>
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
