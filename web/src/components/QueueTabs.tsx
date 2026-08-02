import { Tab, TabGroup, TabList } from '@headlessui/react'
import type { JobStatus, StatusResponse } from '../api/types'
import { capitalize } from '../lib/format'

export type QueueTab = Exclude<JobStatus, 'active'> | 'trash'

// `active` is deliberately not a tab: those jobs are shown live in the
// ActiveStrip above the tab bar instead of a browsable queue.
const UNPROCESSED_TABS: QueueTab[] = ['hold', 'pending']
const RESULTS_TABS: QueueTab[] = ['matched', 'quarantine', 'inconclusive', 'error', 'remediated', 'trash']
const ALL_TABS: QueueTab[] = [...UNPROCESSED_TABS, ...RESULTS_TABS]

interface QueueTabsProps {
  status: StatusResponse | null
  active: QueueTab
  onChange: (status: QueueTab) => void
}

function label(tab: QueueTab): string {
  return tab === 'trash' ? 'Trash' : capitalize(tab)
}

function count(status: StatusResponse | null, tab: QueueTab): number {
  if (tab === 'trash') return status?.trash_count ?? 0
  return status?.queues[tab] ?? 0
}

const TAB_CLASS =
  'flex items-center gap-2 rounded-t-lg px-3 py-2 text-sm font-medium text-slate-400 outline-none data-selected:bg-slate-800 data-selected:text-indigo-400 data-hover:text-slate-200'

function GroupLabel({ children }: { children: string }) {
  return <span className="mr-1 self-center text-xs font-semibold uppercase tracking-wide text-slate-600">{children}</span>
}

/** Two labeled groups within a single tab bar — "Unprocessed" (hold,
 * pending) and "Results" (matched/quarantine/inconclusive/error/remediated,
 * plus Trash). Count badges come from `status.queues`/`status.trash_count`,
 * live-updated by the parent via SSE `stats` events. */
function QueueTabs({ status, active, onChange }: QueueTabsProps) {
  const selectedIndex = ALL_TABS.indexOf(active)

  return (
    <TabGroup selectedIndex={selectedIndex} onChange={(index) => onChange(ALL_TABS[index])}>
      <TabList className="flex flex-wrap items-center gap-1 border-b border-slate-800 px-6 pt-2">
        <GroupLabel>Unprocessed</GroupLabel>
        {UNPROCESSED_TABS.map((tab) => (
          <Tab key={tab} className={TAB_CLASS}>
            {label(tab)}
            <span className="rounded-full bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
              {count(status, tab)}
            </span>
          </Tab>
        ))}
        <span className="mx-2 h-5 w-px bg-slate-800" aria-hidden="true" />
        <GroupLabel>Results</GroupLabel>
        {RESULTS_TABS.map((tab) => (
          <Tab key={tab} className={TAB_CLASS}>
            {label(tab)}
            <span className="rounded-full bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
              {count(status, tab)}
            </span>
          </Tab>
        ))}
      </TabList>
    </TabGroup>
  )
}

export default QueueTabs
