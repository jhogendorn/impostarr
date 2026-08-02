import { useCallback, useEffect, useRef, useState } from 'react'
import { getQueue, getStatus } from './api/client'
import { useEvents } from './api/sse'
import type { JobStatus, QueuePage, SseEvent, StatusResponse } from './api/types'
import InspectModal from './components/InspectModal'
import QueueTable from './components/QueueTable'
import QueueTabs from './components/QueueTabs'
import StatusHeader from './components/StatusHeader'

const PAGE_SIZE = 50
const JOB_UPDATE_DEBOUNCE_MS = 500

function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [activeTab, setActiveTab] = useState<JobStatus>('hold')
  const [pageIndex, setPageIndex] = useState(1)
  const [queuePage, setQueuePage] = useState<QueuePage | null>(null)
  const [inspectJobId, setInspectJobId] = useState<number | null>(null)

  const fetchStatus = useCallback(() => {
    getStatus()
      .then(setStatus)
      .catch((err: unknown) => console.error('status fetch failed', err))
  }, [])

  const fetchQueue = useCallback((jobStatus: JobStatus, page: number) => {
    getQueue(jobStatus, page, PAGE_SIZE)
      .then(setQueuePage)
      .catch((err: unknown) => console.error('queue fetch failed', err))
  }, [])

  useEffect(() => {
    fetchStatus()
  }, [fetchStatus])

  useEffect(() => {
    fetchQueue(activeTab, pageIndex)
  }, [fetchQueue, activeTab, pageIndex])

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
        fetchQueue(activeTab, pageIndex)
        fetchStatus()
      }, JOB_UPDATE_DEBOUNCE_MS)
    },
    [activeTab, pageIndex, fetchQueue, fetchStatus],
  )

  const connected = useEvents(handleEvent)

  useEffect(() => {
    return () => {
      if (debounceTimer.current !== null) clearTimeout(debounceTimer.current)
    }
  }, [])

  function handleTabChange(jobStatus: JobStatus) {
    setActiveTab(jobStatus)
    setPageIndex(1)
  }

  function handleModalChanged() {
    fetchQueue(activeTab, pageIndex)
    fetchStatus()
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <StatusHeader status={status} connected={connected} />
      <QueueTabs status={status} active={activeTab} onChange={handleTabChange} />
      <QueueTable
        page={queuePage}
        pageIndex={pageIndex}
        pageSize={PAGE_SIZE}
        onPageChange={setPageIndex}
        onInspect={setInspectJobId}
      />
      <InspectModal
        jobId={inspectJobId}
        open={inspectJobId !== null}
        onClose={() => setInspectJobId(null)}
        onChanged={handleModalChanged}
      />
    </div>
  )
}

export default App
