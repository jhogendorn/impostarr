import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { JobSummary, QueuePage } from '../api/types'
import QueueTable from './QueueTable'
import { queuePageFixture, statusFixture } from './testFixtures'

const { rerunJobMock, parkJobMock, unparkJobMock } = vi.hoisted(() => ({
  rerunJobMock: vi.fn(),
  parkJobMock: vi.fn(),
  unparkJobMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  rerunJob: rerunJobMock,
  parkJob: parkJobMock,
  unparkJob: unparkJobMock,
}))

const defaultProps = {
  pageIndex: 1,
  pageSize: 50,
  onPageChange: vi.fn(),
  onPageSizeChange: vi.fn(),
  onInspect: vi.fn(),
  instances: statusFixture.instances,
  selectedInstance: undefined,
  onInstanceChange: vi.fn(),
  sortField: 'updated_at' as const,
  sortDir: 'desc' as const,
  onSortChange: vi.fn(),
  onChanged: vi.fn(),
}

function mixedStatusPage(): QueuePage {
  const items: JobSummary[] = [
    { ...queuePageFixture.items[0], job_id: 1, status: 'quarantine' }, // rerun-eligible
    { ...queuePageFixture.items[1], job_id: 2, status: 'pending' }, // park-eligible
    { ...queuePageFixture.items[2], job_id: 3, status: 'hold' }, // unpark-eligible
  ]
  return { total: 3, page_size: 50, items }
}

describe('QueueTable', () => {
  it('renders rows: "Labelled episode" header spans Series+Episode, separate File column, percent Confidence badge', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    expect(screen.getByText('Labelled episode')).toBeInTheDocument()
    expect(screen.getByText('Series 10')).toBeInTheDocument()
    expect(screen.getByText('Show.S01E01.mkv')).toBeInTheDocument()
    expect(screen.getByText('200, 201')).toBeInTheDocument()
    expect(screen.getAllByText('main', { selector: 'td' }).length).toBeGreaterThan(0)
    expect(screen.getByText('backup', { selector: 'td' })).toBeInTheDocument()
    expect(screen.getByText('1–4 of 4')).toBeInTheDocument()
  })

  it('renders Confidence as a rounded percentage, color-coded by band', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    expect(screen.getByText('92%')).toHaveClass('text-emerald-400')
    expect(screen.getByText('50%')).toHaveClass('text-amber-400')
    expect(screen.getByText('10%')).toHaveClass('text-red-400')
    expect(screen.getByText('—', { selector: 'span' })).toHaveClass('text-slate-400')
  })

  it('row click fires onInspect with the job id', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} onInspect={onInspect} />)

    await user.click(screen.getByText('Show.S01E01.mkv'))

    expect(onInspect).toHaveBeenCalledWith(1)
  })

  it('has no Inspect action button in the row (the row click already inspects)', async () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    expect(screen.queryByRole('button', { name: 'Inspect' })).not.toBeInTheDocument()
  })

  it('clicking a row checkbox toggles its checked DOM state and does not open inspect', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} onInspect={onInspect} />)

    const checkbox = screen.getByLabelText('Select job 1') as HTMLInputElement
    expect(checkbox.checked).toBe(false)

    await user.click(checkbox)

    expect(checkbox.checked).toBe(true)
    expect(onInspect).not.toHaveBeenCalled()
  })

  it('checkboxes carry dark-appropriate styling (sized, accent-colored)', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    const checkbox = screen.getByLabelText('Select job 1')
    expect(checkbox).toHaveClass('accent-indigo-500')
    expect(checkbox).toHaveClass('h-4', 'w-4')
  })

  it('changing the page size fires onPageSizeChange', async () => {
    const user = userEvent.setup()
    const onPageSizeChange = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} onPageSizeChange={onPageSizeChange} />)

    await user.selectOptions(screen.getByLabelText('Page size'), '100')

    expect(onPageSizeChange).toHaveBeenCalledWith(100)
  })

  it('the page-size select sits in the same bottom bar as the pagination range and Prev/Next', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    const bar = screen.getByLabelText('Page size').closest('div')!.parentElement!
    expect(within(bar).getByText('1–4 of 4')).toBeInTheDocument()
    expect(within(bar).getByRole('button', { name: 'Prev' })).toBeInTheDocument()
    expect(within(bar).getByRole('button', { name: 'Next' })).toBeInTheDocument()
  })

  it('hides the instance filter when there is only one instance', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} instances={[statusFixture.instances[0]]} />)

    expect(screen.queryByLabelText('Instance')).not.toBeInTheDocument()
  })

  it('the instance filter fires onInstanceChange when multiple instances exist', async () => {
    const user = userEvent.setup()
    const onInstanceChange = vi.fn()
    const twoInstances = [statusFixture.instances[0], { ...statusFixture.instances[0], name: 'backup' }]
    render(
      <QueueTable
        page={queuePageFixture}
        {...defaultProps}
        instances={twoInstances}
        onInstanceChange={onInstanceChange}
      />,
    )

    await user.selectOptions(screen.getByLabelText('Instance'), 'backup')

    expect(onInstanceChange).toHaveBeenCalledWith('backup')
  })

  it('clicking the active sort field header (Updated) toggles its direction', async () => {
    const user = userEvent.setup()
    const onSortChange = vi.fn()
    render(
      <QueueTable
        page={queuePageFixture}
        {...defaultProps}
        sortField="updated_at"
        sortDir="desc"
        onSortChange={onSortChange}
      />,
    )

    await user.click(screen.getByRole('button', { name: /Updated/ }))

    expect(onSortChange).toHaveBeenCalledWith('updated_at', 'asc')
    // active field shows a bright directional arrow
    expect(screen.getByRole('button', { name: /Updated/ })).toHaveTextContent('▼')
  })

  it.each([
    ['Series', 'series'],
    ['Instance', 'instance'],
    ['Confidence', 'confidence'],
  ] as const)('clicking the %s header switches sort to %s, defaulting to desc', async (label, field) => {
    const user = userEvent.setup()
    const onSortChange = vi.fn()
    render(
      <QueueTable
        page={queuePageFixture}
        {...defaultProps}
        sortField="updated_at"
        sortDir="desc"
        onSortChange={onSortChange}
      />,
    )

    await user.click(screen.getByRole('button', { name: new RegExp(label) }))

    expect(onSortChange).toHaveBeenCalledWith(field, 'desc')
  })

  it('inactive sortable headers show a dim neutral indicator, not the active arrow', () => {
    render(<QueueTable page={queuePageFixture} {...defaultProps} sortField="updated_at" sortDir="desc" />)

    expect(screen.getByRole('button', { name: /Series \(id\)/ })).toHaveTextContent('⇅')
    expect(screen.getByRole('button', { name: /Instance/ })).toHaveTextContent('⇅')
    expect(screen.getByRole('button', { name: /Confidence/ })).toHaveTextContent('⇅')
  })

  it('the free-text filter narrows rows by path or series id, client-side over the current page', async () => {
    const user = userEvent.setup()
    render(<QueueTable page={queuePageFixture} {...defaultProps} />)

    await user.type(screen.getByPlaceholderText(/Filter by path/), 'Show2')

    expect(screen.getByText('Show2.S02E02.mkv')).toBeInTheDocument()
    expect(screen.queryByText('Show.S01E01.mkv')).not.toBeInTheDocument()
  })

  it('selecting rows shows the bulk action bar; ineligible actions are disabled', async () => {
    const user = userEvent.setup()
    render(<QueueTable page={mixedStatusPage()} {...defaultProps} />)

    expect(screen.queryByText(/selected/)).not.toBeInTheDocument()

    await user.click(screen.getByLabelText('Select job 1')) // quarantine: rerun-eligible only

    expect(screen.getByText('1 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rerun (1)' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Park (0)' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Unpark (0)' })).toBeDisabled()
  })

  it('bulk Rerun calls the API only for eligible selected rows, sequentially, then refetches', async () => {
    rerunJobMock.mockResolvedValue({ result: 'pending' })
    const user = userEvent.setup()
    const onChanged = vi.fn()
    render(<QueueTable page={mixedStatusPage()} {...defaultProps} onChanged={onChanged} />)

    await user.click(screen.getByLabelText('Select job 1')) // quarantine: eligible
    await user.click(screen.getByLabelText('Select job 2')) // pending: not rerun-eligible
    await user.click(screen.getByRole('button', { name: /^Rerun \(/ }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(rerunJobMock).toHaveBeenCalledTimes(1)
    expect(rerunJobMock).toHaveBeenCalledWith(1)
  })

  it('bulk Park calls the API only for the pending row', async () => {
    parkJobMock.mockResolvedValue({ result: 'hold' })
    const user = userEvent.setup()
    const onChanged = vi.fn()
    render(<QueueTable page={mixedStatusPage()} {...defaultProps} onChanged={onChanged} />)

    await user.click(screen.getByLabelText('Select all'))
    await user.click(screen.getByRole('button', { name: /^Park \(/ }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(parkJobMock).toHaveBeenCalledTimes(1)
    expect(parkJobMock).toHaveBeenCalledWith(2)
  })

  it('bulk Unpark calls the API only for the hold row and reports errors inline', async () => {
    unparkJobMock.mockRejectedValue(new Error('409: job is no longer hold'))
    const user = userEvent.setup()
    const onChanged = vi.fn()
    render(<QueueTable page={mixedStatusPage()} {...defaultProps} onChanged={onChanged} />)

    await user.click(screen.getByLabelText('Select all'))
    await user.click(screen.getByRole('button', { name: /^Unpark \(/ }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(unparkJobMock).toHaveBeenCalledTimes(1)
    expect(unparkJobMock).toHaveBeenCalledWith(3)
    expect(await screen.findByText(/job 3: 409/)).toBeInTheDocument()
  })
})
