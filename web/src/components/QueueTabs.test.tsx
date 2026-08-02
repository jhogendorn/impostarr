import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import QueueTabs from './QueueTabs'
import { statusFixture } from './testFixtures'

const EXPECTED_TABS: [label: string, count: number][] = [
  ['Hold', 1],
  ['Pending', 2],
  ['Matched', 4],
  ['Quarantine', 5],
  ['Inconclusive', 6],
  ['Error', 7],
  ['Remediated', 8],
  ['Trash', 2],
]

describe('QueueTabs', () => {
  it('renders 8 tabs (no "Active" tab) grouped under Queued/Processed, with counts from status', () => {
    render(<QueueTabs status={statusFixture} active="hold" onChange={vi.fn()} />)

    expect(screen.getAllByRole('tab')).toHaveLength(8)
    expect(screen.queryByRole('tab', { name: /^Active/ })).not.toBeInTheDocument()
    for (const [label, count] of EXPECTED_TABS) {
      const tab = screen.getByRole('tab', { name: new RegExp(`^${label}`) })
      expect(tab).toHaveTextContent(String(count))
    }
  })

  it('renders the Queued and Processed group labels, styled as non-interactive text above each cluster', () => {
    render(<QueueTabs status={statusFixture} active="hold" onChange={vi.fn()} />)

    const queuedLabel = screen.getByText('Queued')
    const processedLabel = screen.getByText('Processed')
    expect(queuedLabel).toHaveClass('text-xs', 'uppercase', 'text-slate-500')
    expect(processedLabel).toHaveClass('text-xs', 'uppercase', 'text-slate-500')
    expect(queuedLabel.tagName).not.toBe('BUTTON')
    expect(processedLabel.tagName).not.toBe('BUTTON')
  })

  it('the Trash tab shows trash_count', () => {
    render(<QueueTabs status={statusFixture} active="hold" onChange={vi.fn()} />)

    expect(screen.getByRole('tab', { name: /^Trash/ })).toHaveTextContent('2')
  })

  it('clicking a tab fires onChange with the clicked status', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<QueueTabs status={statusFixture} active="hold" onChange={onChange} />)

    await user.click(screen.getByRole('tab', { name: /^Quarantine/ }))

    expect(onChange).toHaveBeenCalledWith('quarantine')
  })

  it('clicking the Trash tab fires onChange with "trash"', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<QueueTabs status={statusFixture} active="hold" onChange={onChange} />)

    await user.click(screen.getByRole('tab', { name: /^Trash/ }))

    expect(onChange).toHaveBeenCalledWith('trash')
  })
})
