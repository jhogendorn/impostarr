import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import StatusHeader from './StatusHeader'
import { dryRunStatusFixture, pausedStatusFixture, statusFixture } from './testFixtures'

describe('StatusHeader', () => {
  it('does not show a DRY RUN badge when status.dry_run is false', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.queryByTestId('dry-run-badge')).not.toBeInTheDocument()
  })

  it('shows a DRY RUN badge when status.dry_run is true', () => {
    render(<StatusHeader status={dryRunStatusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByTestId('dry-run-badge')).toHaveTextContent(/dry run/i)
  })

  it('does not show a DRY RUN badge when status is null', () => {
    render(<StatusHeader status={null} connected={false} onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.queryByTestId('dry-run-badge')).not.toBeInTheDocument()
  })

  it('clicking the Logs button fires onToggleLogs', async () => {
    const user = userEvent.setup()
    const onToggleLogs = vi.fn()
    render(<StatusHeader status={statusFixture} connected onToggleLogs={onToggleLogs} onTogglePause={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Logs' }))

    expect(onToggleLogs).toHaveBeenCalledTimes(1)
  })

  it('shows the queued/processed summary from status.summary', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByText('6 queued · 30 processed')).toBeInTheDocument()
  })

  it('renders an instance chip with a last-sync/last-backfill tooltip', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByText('main').closest('[title]')).toHaveAttribute(
      'title',
      expect.stringMatching(/last sync .*; last backfill .*/),
    )
  })

  it('the connection dot carries an explanatory tooltip', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByTestId('sse-dot').closest('[title]')).toHaveAttribute(
      'title',
      expect.stringContaining('Live updates connected/disconnected'),
    )
  })

  it('does not show a PAUSED badge when status.paused is false', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.queryByTestId('paused-badge')).not.toBeInTheDocument()
  })

  it('shows an amber PAUSED badge when status.paused is true', () => {
    render(<StatusHeader status={pausedStatusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByTestId('paused-badge')).toHaveTextContent(/paused/i)
  })

  it('shows a Pause button when not paused, and clicking it fires onTogglePause', async () => {
    const user = userEvent.setup()
    const onTogglePause = vi.fn()
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={onTogglePause} />)

    const button = screen.getByRole('button', { name: 'Pause' })
    await user.click(button)

    expect(onTogglePause).toHaveBeenCalledTimes(1)
  })

  it('shows a Resume button when paused', () => {
    render(<StatusHeader status={pausedStatusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })

  it('shows the refsubs quota usage with an explanatory tooltip', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    const quota = screen.getByText('refsubs: 3/20')
    expect(quota).toHaveAttribute(
      'title',
      expect.stringContaining('Reference-subtitle fetches used today: 3 of 20'),
    )
  })

  it('shows a placeholder when refsubs_quota is null', () => {
    const noQuota = { ...statusFixture, refsubs_quota: null }
    render(<StatusHeader status={noQuota} connected onToggleLogs={vi.fn()} onTogglePause={vi.fn()} />)

    expect(screen.getByText('refsubs: —')).toBeInTheDocument()
  })
})
