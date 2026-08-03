import { useCallback, useEffect, useRef, useState } from 'react'
import { getQueue, getStatus, pauseWorkers, resumeWorkers } from './api/client'
import { useEvents } from './api/sse'
import type { QueuePage, QueueSortField, SortDir, SseEvent, StatusResponse } from './api/types'
import ActiveStrip from './components/ActiveStrip'
import InspectModal from './components/InspectModal'
import LogDrawer from './components/LogDrawer'
import QueueTable from './components/QueueTable'
import QueueTabs, { type QueueTab } from './components/QueueTabs'
import StatusHeader from './components/StatusHeader'
import TrashTable from './components/TrashTable'

const DEFAULT_PAGE_SIZE = 50
const JOB_UPDATE_DEBOUNCE_MS = 500

function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [activeTab, setActiveTab] = useState<QueueTab>('hold')
  const [pageIndex, setPageIndex] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [instanceFilter, setInstanceFilter] = useState<string | undefined>(undefined)
  const [sortField, setSortField] = useState<QueueSortField>('updated_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [queuePage, setQueuePage] = useState<QueuePage | null>(null)
  const [inspectJobId, setInspectJobId] = useState<number | null>(null)
  const [logsOpen, setLogsOpen] = useState(false)

  // Latest fetch-params, readable from the debounce timer callback at fire
  // time rather than captured (possibly stale) at schedule time.
  const activeTabRef = useRef(activeTab)
  activeTabRef.current = activeTab
  const pageIndexRef = useRef(pageIndex)
  pageIndexRef.current = pageIndex
  const pageSizeRef = useRef(pageSize)
  pageSizeRef.current = pageSize
  const instanceFilterRef = useRef(instanceFilter)
  instanceFilterRef.current = instanceFilter
  const sortFieldRef = useRef(sortField)
  sortFieldRef.current = sortField
  const sortDirRef = useRef(sortDir)
  sortDirRef.current = sortDir

  // Per-fetch-kind request tokens: a response only calls setState if it's
  // still the most recently issued request of its kind — guards against an
  // older, slower request resolving after a newer one (e.g. two queue
  // fetches in flight for different tabs).
  const statusTokenRef = useRef(0)
  const queueTokenRef = useRef(0)

  const fetchStatus = useCallback(() => {
    const token = ++statusTokenRef.current
    getStatus()
      .then((data) => {
        if (token === statusTokenRef.current) setStatus(data)
      })
      .catch((err: unknown) => console.error('status fetch failed', err))
  }, [])

  const fetchQueue = useCallback(
    (
      tab: QueueTab,
      page: number,
      size: number,
      instance: string | undefined,
      field: QueueSortField,
      dir: SortDir,
    ) => {
      if (tab === 'trash') return // TrashTable owns its own fetch, /trash isn't a job-status queue
      const token = ++queueTokenRef.current
      getQueue(tab, { page, pageSize: size, instance, sort: field, dir })
        .then((data) => {
          if (token === queueTokenRef.current) setQueuePage(data)
        })
        .catch((err: unknown) => console.error('queue fetch failed', err))
    },
    [],
  )

  useEffect(() => {
    fetchStatus()
  }, [fetchStatus])

  useEffect(() => {
    fetchQueue(activeTab, pageIndex, pageSize, instanceFilter, sortField, sortDir)
  }, [fetchQueue, activeTab, pageIndex, pageSize, instanceFilter, sortField, sortDir])

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // job_update → debounced queue+status refresh (a burst of updates during
  // a pipeline run shouldn't hammer the API); stats → direct status update,
  // no refetch needed since the event already carries fresh queue counts.
  const handleEvent = useCallback(
    (event: SseEvent) => {
      if (event.kind === 'stats') {
        setStatus((prev) => (prev ? { ...prev, queues: event.data } : prev))
        return
      }
      if (debounceTimer.current !== null) clearTimeout(debounceTimer.current)
      debounceTimer.current = setTimeout(() => {
        debounceTimer.current = null
        fetchQueue(
          activeTabRef.current,
          pageIndexRef.current,
          pageSizeRef.current,
          instanceFilterRef.current,
          sortFieldRef.current,
          sortDirRef.current,
        )
        fetchStatus()
      }, JOB_UPDATE_DEBOUNCE_MS)
    },
    [fetchQueue, fetchStatus],
  )

  const connected = useEvents(handleEvent)

  useEffect(() => {
    return () => {
      if (debounceTimer.current !== null) clearTimeout(debounceTimer.current)
    }
  }, [])

  function handleTabChange(tab: QueueTab) {
    setActiveTab(tab)
    setPageIndex(1)
  }

  function handlePageSizeChange(size: number) {
    setPageSize(size)
    setPageIndex(1)
  }

  function handleInstanceChange(instance: string | undefined) {
    setInstanceFilter(instance)
    setPageIndex(1)
  }

  function handleSortChange(field: QueueSortField, dir: SortDir) {
    setSortField(field)
    setSortDir(dir)
    setPageIndex(1)
  }

  function handleModalChanged() {
    fetchQueue(activeTab, pageIndex, pageSize, instanceFilter, sortField, sortDir)
    fetchStatus()
  }

  function handleTogglePause() {
    const action = status?.paused ? resumeWorkers() : pauseWorkers()
    action.then(fetchStatus).catch((err: unknown) => console.error('pause/resume failed', err))
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <StatusHeader
        status={status}
        connected={connected}
        onToggleLogs={() => setLogsOpen((open) => !open)}
        onTogglePause={handleTogglePause}
      />
      <ActiveStrip activeJobs={status?.active_jobs ?? []} system={status?.system} />
      {/* One inset rounded card for the whole queue area — tab bar, filter
       * row, table, and pagination together — matching the Active section's
       * inset. Each child keeps its own internal padding; only the shared
       * card carries the ring, so nothing inside re-glows its own edges. */}
      <section className="px-6 pt-4">
        <div className="glow-panel rounded-xl bg-slate-900/40">
          <QueueTabs status={status} active={activeTab} onChange={handleTabChange} />
          {activeTab === 'trash' ? (
            <TrashTable />
          ) : (
            <QueueTable
              page={queuePage}
              pageIndex={pageIndex}
              pageSize={pageSize}
              onPageChange={setPageIndex}
              onPageSizeChange={handlePageSizeChange}
              onInspect={setInspectJobId}
              instances={status?.instances ?? []}
              selectedInstance={instanceFilter}
              onInstanceChange={handleInstanceChange}
              sortField={sortField}
              sortDir={sortDir}
              onSortChange={handleSortChange}
              onChanged={handleModalChanged}
            />
          )}
        </div>
      </section>
      <InspectModal
        jobId={inspectJobId}
        open={inspectJobId !== null}
        onClose={() => setInspectJobId(null)}
        onChanged={handleModalChanged}
        dryRun={status?.dry_run ?? false}
      />
      <LogDrawer open={logsOpen} />
    </div>
  )
}

export default App
