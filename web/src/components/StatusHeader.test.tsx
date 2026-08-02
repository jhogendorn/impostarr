import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import StatusHeader from './StatusHeader'
import { dryRunStatusFixture, statusFixture } from './testFixtures'

describe('StatusHeader', () => {
  it('does not show a DRY RUN badge when status.dry_run is false', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} />)

    expect(screen.queryByTestId('dry-run-badge')).not.toBeInTheDocument()
  })

  it('shows a DRY RUN badge when status.dry_run is true', () => {
    render(<StatusHeader status={dryRunStatusFixture} connected onToggleLogs={vi.fn()} />)

    expect(screen.getByTestId('dry-run-badge')).toHaveTextContent(/dry run/i)
  })

  it('does not show a DRY RUN badge when status is null', () => {
    render(<StatusHeader status={null} connected={false} onToggleLogs={vi.fn()} />)

    expect(screen.queryByTestId('dry-run-badge')).not.toBeInTheDocument()
  })

  it('clicking the Logs button fires onToggleLogs', async () => {
    const user = userEvent.setup()
    const onToggleLogs = vi.fn()
    render(<StatusHeader status={statusFixture} connected onToggleLogs={onToggleLogs} />)

    await user.click(screen.getByRole('button', { name: 'Logs' }))

    expect(onToggleLogs).toHaveBeenCalledTimes(1)
  })

  it('shows the queued/processed summary from status.summary', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} />)

    expect(screen.getByText('6 queued · 30 processed')).toBeInTheDocument()
  })

  it('renders an instance chip with a last-sync/last-backfill tooltip', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} />)

    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByText('main').closest('[title]')).toHaveAttribute(
      'title',
      expect.stringMatching(/last sync .*; last backfill .*/),
    )
  })

  it('the connection dot carries an explanatory tooltip', () => {
    render(<StatusHeader status={statusFixture} connected onToggleLogs={vi.fn()} />)

    expect(screen.getByTestId('sse-dot').closest('[title]')).toHaveAttribute(
      'title',
      expect.stringContaining('Live updates connected/disconnected'),
    )
  })
})
