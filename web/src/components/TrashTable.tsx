import { useEffect, useRef, useState } from 'react'
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react'
import { deleteTrashItem, getTrash, restoreTrashItem } from '../api/client'
import type { TrashPage } from '../api/types'
import { formatCountdown, pathBasename } from '../lib/format'

const PAGE_SIZE = 50
const COUNTDOWN_TICK_MS = 1000

/** Trash tab: lists `TrashItem` rows with a ticking expires-in countdown,
 * Restore and Delete-now actions (delete-now behind a confirm dialog since
 * it's an irreversible, other-party-invisible-but-user-visible mutation of
 * the operator's own trash mount). Owns its own fetch — unlike QueueTable,
 * trash isn't part of the job-status queue paging the parent already
 * drives. */
function TrashTable() {
  const [page, setPage] = useState<TrashPage | null>(null)
  const [pageIndex, setPageIndex] = useState(1)
  const [now, setNow] = useState(() => Date.now())
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  // `expires_in_s` is a snapshot as of the last fetch — this anchors it so
  // the countdown can keep ticking client-side between fetches, rather than
  // freezing until the next page load.
  const fetchedAtMsRef = useRef(Date.now())

  function refetch() {
    getTrash(pageIndex, PAGE_SIZE)
      .then((data) => {
        fetchedAtMsRef.current = Date.now()
        setPage(data)
      })
      .catch((err: unknown) => console.error('trash fetch failed', err))
  }

  useEffect(refetch, [pageIndex])

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), COUNTDOWN_TICK_MS)
    return () => clearInterval(timer)
  }, [])

  function secondsRemaining(expiresInSAtFetch: number): number {
    return expiresInSAtFetch - (now - fetchedAtMsRef.current) / 1000
  }

  const items = page?.items ?? []
  const total = page?.total ?? 0
  const rangeStart = total === 0 ? 0 : (pageIndex - 1) * PAGE_SIZE + 1
  const rangeEnd = Math.min(pageIndex * PAGE_SIZE, total)
  const hasNext = pageIndex * PAGE_SIZE < total

  async function handleRestore(id: number) {
    setPendingId(id)
    setError(null)
    try {
      await restoreTrashItem(id)
      refetch()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPendingId(null)
    }
  }

  async function handleDeleteConfirmed() {
    if (confirmDeleteId === null) return
    const id = confirmDeleteId
    setConfirmDeleteId(null)
    setPendingId(id)
    setError(null)
    try {
      await deleteTrashItem(id)
      refetch()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setPendingId(null)
    }
  }

  return (
    <div className="px-6 py-4">
      {error && (
        <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </p>
      )}
      <table className="w-full border-separate border-spacing-y-1 text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="px-3 py-2">File</th>
            <th className="px-3 py-2">Instance</th>
            <th className="px-3 py-2">Series / Episodes</th>
            <th className="px-3 py-2">Size</th>
            <th className="px-3 py-2">Trashed</th>
            <th className="px-3 py-2">Expires in</th>
            <th className="px-3 py-2">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className="rounded-lg bg-slate-800/60">
              <td className="rounded-l-lg px-3 py-2 text-slate-200">{pathBasename(item.original_path)}</td>
              <td className="px-3 py-2 text-slate-300">{item.instance}</td>
              <td className="px-3 py-2 text-slate-400">
                {item.series_id} · {item.episode_ids.join(', ')}
              </td>
              <td className="px-3 py-2 text-slate-400">{(item.size / 1024 / 1024).toFixed(1)} MB</td>
              <td className="px-3 py-2 text-slate-500">{new Date(item.trashed_at).toLocaleString()}</td>
              <td className="px-3 py-2 text-slate-400">{formatCountdown(secondsRemaining(item.expires_in_s))}</td>
              <td className="rounded-r-lg px-3 py-2">
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={pendingId === item.id}
                    onClick={() => void handleRestore(item.id)}
                    className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-40"
                  >
                    Restore
                  </button>
                  <button
                    type="button"
                    disabled={pendingId === item.id}
                    onClick={() => setConfirmDeleteId(item.id)}
                    className="rounded border border-red-500/40 px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-40"
                  >
                    Delete now
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {items.length === 0 && <p className="px-3 py-6 text-sm text-slate-500">Trash is empty.</p>}
      <div className="mt-3 flex items-center justify-between text-sm text-slate-400">
        <span>
          {rangeStart}–{rangeEnd} of {total}
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={pageIndex <= 1}
            onClick={() => setPageIndex((p) => p - 1)}
            className="rounded-lg border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            Prev
          </button>
          <button
            type="button"
            disabled={!hasNext}
            onClick={() => setPageIndex((p) => p + 1)}
            className="rounded-lg border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            Next
          </button>
        </div>
      </div>

      <Dialog open={confirmDeleteId !== null} onClose={() => setConfirmDeleteId(null)} className="relative z-50">
        <DialogBackdrop className="fixed inset-0 bg-black/70" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-sm rounded-lg border border-slate-700 bg-slate-900 p-6 text-slate-100">
            <DialogTitle className="text-base font-semibold text-red-400">Delete permanently?</DialogTitle>
            <p className="mt-2 text-sm text-slate-400">
              This permanently deletes the trashed file. This cannot be undone.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDeleteId(null)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleDeleteConfirmed()}
                className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-sm text-red-400 hover:bg-red-500/20"
              >
                Delete now
              </button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </div>
  )
}

export default TrashTable
