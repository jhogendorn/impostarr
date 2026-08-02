import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LogDrawer from './LogDrawer'
import { logRecordsFixture } from './testFixtures'

const { getLogsMock } = vi.hoisted(() => ({ getLogsMock: vi.fn() }))

vi.mock('../api/client', () => ({
  getLogs: getLogsMock,
}))

describe('LogDrawer', () => {
  beforeEach(() => {
    getLogsMock.mockResolvedValue({ items: logRecordsFixture })
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('renders nothing when closed', () => {
    render(<LogDrawer open={false} />)

    expect(getLogsMock).not.toHaveBeenCalled()
    expect(screen.queryByText('Logs')).not.toBeInTheDocument()
  })

  it('fetches and renders log lines from the fixture when open', async () => {
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })

    expect(getLogsMock).toHaveBeenCalledWith('INFO')
    expect(screen.getByText(/claimed job 42/)).toBeInTheDocument()
    expect(screen.getByText(/plugin crashed/)).toBeInTheDocument()
  })

  it('a DRY-RUN line gets the amber highlight class', async () => {
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })

    const dryRunLine = screen.getByText(/DRY-RUN: would DELETE/)
    expect(dryRunLine).toHaveClass('text-amber-400')

    const normalLine = screen.getByText(/claimed job 42/)
    expect(normalLine).toHaveClass('text-slate-300')
  })

  it('changing the level filter refetches with the new level', async () => {
    const user = userEvent.setup()
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })
    getLogsMock.mockClear()

    await user.selectOptions(screen.getByLabelText('Level'), 'ERROR')

    expect(getLogsMock).toHaveBeenCalledWith('ERROR')
  })
})
