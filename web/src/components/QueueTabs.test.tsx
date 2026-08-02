import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { JOB_STATUSES } from '../api/types'
import QueueTabs from './QueueTabs'
import { statusFixture } from './testFixtures'

describe('QueueTabs', () => {
  it('renders all 8 tabs with counts from the status fixture', () => {
    render(<QueueTabs status={statusFixture} active="hold" onChange={vi.fn()} />)

    expect(screen.getAllByRole('tab')).toHaveLength(8)
    for (const jobStatus of JOB_STATUSES) {
      const label = jobStatus[0].toUpperCase() + jobStatus.slice(1)
      const tab = screen.getByRole('tab', { name: new RegExp(`^${label}`) })
      expect(tab).toHaveTextContent(String(statusFixture.queues[jobStatus]))
    }
  })

  it('clicking a tab fires onChange with the clicked status', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<QueueTabs status={statusFixture} active="hold" onChange={onChange} />)

    await user.click(screen.getByRole('tab', { name: /^Quarantine/ }))

    expect(onChange).toHaveBeenCalledWith('quarantine')
  })
})
