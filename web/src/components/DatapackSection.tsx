import { useState } from 'react'
import { datapackUrl } from '../api/client'

/** Section F: debug datapack download — de-emphasised, bare checkbox (no
 * visible label text — an aria-label covers screen readers only) plus the
 * download control, side by side. Checkbox arms the button/link. */
function DatapackSection({ jobId }: { jobId: number }) {
  const [armed, setArmed] = useState(false)
  return (
    <section className="flex items-center gap-2 border-t border-slate-800 pt-4 text-xs text-slate-500">
      <input
        type="checkbox"
        aria-label="Enable debug datapack download"
        checked={armed}
        onChange={(event) => setArmed(event.target.checked)}
        className="h-3.5 w-3.5 accent-indigo-500"
      />
      {armed ? (
        <a
          href={datapackUrl(jobId)}
          download
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
        >
          Download Debug Datapack
        </a>
      ) : (
        <button
          type="button"
          disabled
          className="rounded-lg border border-slate-800 px-3 py-1.5 text-slate-600 disabled:cursor-not-allowed"
        >
          Download Debug Datapack
        </button>
      )}
    </section>
  )
}

export default DatapackSection
