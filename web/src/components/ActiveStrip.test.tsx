import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ActiveStrip from './ActiveStrip'
import { activeJobsFixture, statusFixture } from './testFixtures'

describe('ActiveStrip', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-08-03T00:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows an idle state and the system meter when there are no active jobs', () => {
    render(<ActiveStrip activeJobs={[]} system={statusFixture.system} />)

    expect(screen.getByText(/idle/i)).toBeInTheDocument()
    expect(screen.getByText('CPU 43%')).toBeInTheDocument()
    expect(screen.getByText('MEM 61%')).toBeInTheDocument()
  })

  it('renders a card per active job with instance, path, claimed_by, and elapsed', () => {
    render(<ActiveStrip activeJobs={activeJobsFixture} system={statusFixture.system} />)

    expect(screen.getByText(/main · Show\.S01E01\.mkv/)).toBeInTheDocument()
    expect(screen.getByText('worker-1')).toBeInTheDocument()
    expect(screen.getByText('worker-2')).toBeInTheDocument()
    // job 1 claimed_at 23:59:00Z, now 00:00:00Z -> 60s elapsed
    expect(screen.getByText('1m 00s')).toBeInTheDocument()
    // job 2 claimed_at 23:58:00Z -> 120s elapsed
    expect(screen.getByText('2m 00s')).toBeInTheDocument()
  })

  it('ticks the elapsed time forward every second while jobs are active', () => {
    render(<ActiveStrip activeJobs={[activeJobsFixture[0]]} system={statusFixture.system} />)

    expect(screen.getByText('1m 00s')).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(5000)
    })

    expect(screen.getByText('1m 05s')).toBeInTheDocument()
  })
})
