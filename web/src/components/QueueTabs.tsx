import { Tab, TabGroup, TabList } from '@headlessui/react'
import { JOB_STATUSES, type JobStatus, type StatusResponse } from '../api/types'

interface QueueTabsProps {
  status: StatusResponse | null
  active: JobStatus
  onChange: (status: JobStatus) => void
}

function label(status: JobStatus): string {
  return status[0].toUpperCase() + status.slice(1)
}

/** Tab per job status (8 tabs — no combined "history" endpoint exists, so
 * remediated/error get their own tabs rather than a synthetic merge).
 * Count badges come from `status.queues`, live-updated by the parent via
 * SSE `stats` events. */
function QueueTabs({ status, active, onChange }: QueueTabsProps) {
  const selectedIndex = JOB_STATUSES.indexOf(active)

  return (
    <TabGroup
      selectedIndex={selectedIndex}
      onChange={(index) => onChange(JOB_STATUSES[index])}
    >
      <TabList className="flex flex-wrap gap-1 border-b border-slate-800 px-6 pt-2">
        {JOB_STATUSES.map((jobStatus) => (
          <Tab
            key={jobStatus}
            className="flex items-center gap-2 rounded-t-lg px-3 py-2 text-sm font-medium text-slate-400 outline-none data-selected:bg-slate-800 data-selected:text-indigo-400 data-hover:text-slate-200"
          >
            {label(jobStatus)}
            <span className="rounded-full bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
              {status?.queues[jobStatus] ?? 0}
            </span>
          </Tab>
        ))}
      </TabList>
    </TabGroup>
  )
}

export default QueueTabs
