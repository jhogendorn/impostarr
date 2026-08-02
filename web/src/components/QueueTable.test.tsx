import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import QueueTable from './QueueTable'
import { queuePageFixture } from './testFixtures'

describe('QueueTable', () => {
  it('renders rows from a QueuePage fixture', () => {
    render(
      <QueueTable page={queuePageFixture} pageIndex={1} pageSize={50} onPageChange={vi.fn()} onInspect={vi.fn()} />,
    )

    expect(screen.getByText('Show.S01E01.mkv')).toBeInTheDocument()
    expect(screen.getByText('100')).toBeInTheDocument()
    expect(screen.getByText('200, 201')).toBeInTheDocument()
    expect(screen.getByText('1–4 of 4')).toBeInTheDocument()
  })

  it('color-codes the score badge by band (high/mid/low spot-check)', () => {
    render(
      <QueueTable page={queuePageFixture} pageIndex={1} pageSize={50} onPageChange={vi.fn()} onInspect={vi.fn()} />,
    )

    expect(screen.getByText('0.92')).toHaveClass('text-emerald-400')
    expect(screen.getByText('0.50')).toHaveClass('text-amber-400')
    expect(screen.getByText('0.10')).toHaveClass('text-red-400')
    expect(screen.getByText('—', { selector: 'span' })).toHaveClass('text-slate-400')
  })

  it('row click fires onInspect with the job id', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn()
    render(
      <QueueTable
        page={queuePageFixture}
        pageIndex={1}
        pageSize={50}
        onPageChange={vi.fn()}
        onInspect={onInspect}
      />,
    )

    await user.click(screen.getByText('Show.S01E01.mkv'))

    expect(onInspect).toHaveBeenCalledWith(1)
  })
})
