import { act, render, screen, within } from '@testing-library/react'
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

  it('renders each line structured: level chip, hh:mm:ss time, dimmed logger, message', async () => {
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })

    const line = screen.getByText('claimed job 42').closest('div')!
    expect(within(line).getByText('INFO')).toBeInTheDocument()
    expect(within(line).getByText(/^\d{2}:\d{2}:\d{2}$/)).toBeInTheDocument()
    const loggerEl = within(line).getByText('impostarr.worker')
    expect(loggerEl).toHaveClass('text-slate-600')
  })

  it('a DRY-RUN line gets the amber highlight class', async () => {
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })

    const dryRunLine = screen.getByText(/DRY-RUN: would DELETE/).closest('div')!
    expect(dryRunLine).toHaveClass('text-amber-400')

    const normalLine = screen.getByText('claimed job 42').closest('div')!
    expect(normalLine).toHaveClass('text-slate-300')
  })

  it('renders the level filter as chip buttons, INFO active by default', async () => {
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByRole('button', { name: 'INFO' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'WARNING' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'ERROR' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('clicking a level chip refetches with the new level', async () => {
    const user = userEvent.setup()
    render(<LogDrawer open />)

    await act(async () => {
      await Promise.resolve()
    })
    getLogsMock.mockClear()

    await user.click(screen.getByRole('button', { name: 'ERROR' }))

    expect(getLogsMock).toHaveBeenCalledWith('ERROR')
    expect(screen.getByRole('button', { name: 'ERROR' })).toHaveAttribute('aria-pressed', 'true')
  })
})
