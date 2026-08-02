import { useCallback, useEffect, useRef, useState } from 'react'
import { getQueue, getStatus } from './api/client'
import { useEvents } from './api/sse'
import type { QueuePage, SortDir, SseEvent, StatusResponse } from './api/types'
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
    (tab: QueueTab, page: number, size: number, instance: string | undefined, dir: SortDir) => {
      if (tab === 'trash') return // TrashTable owns its own fetch, /trash isn't a job-status queue
      const token = ++queueTokenRef.current
      getQueue(tab, { page, pageSize: size, instance, sort: 'updated_at', dir })
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
    fetchQueue(activeTab, pageIndex, pageSize, instanceFilter, sortDir)
  }, [fetchQueue, activeTab, pageIndex, pageSize, instanceFilter, sortDir])

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
        fetchQueue(activeTabRef.current, pageIndexRef.current, pageSizeRef.current, instanceFilterRef.current, sortDirRef.current)
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

  function handleModalChanged() {
    fetchQueue(activeTab, pageIndex, pageSize, instanceFilter, sortDir)
    fetchStatus()
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <StatusHeader status={status} connected={connected} onToggleLogs={() => setLogsOpen((open) => !open)} />
      <ActiveStrip activeJobs={status?.active_jobs ?? []} system={status?.system} />
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
          sortDir={sortDir}
          onSortDirChange={setSortDir}
          onChanged={handleModalChanged}
        />
      )}
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
