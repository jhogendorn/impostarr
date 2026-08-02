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
  sortDir: 'desc' as const,
  onSortDirChange: vi.fn(),
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
  it('renders rows: "Labelled episode" header spans Series+Episode, Instance, percent Confidence badge, Updated', () => {
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

  it('the Inspect action button also fires onInspect without double-firing the row handler', async () => {
    const user = userEvent.setup()
    const onInspect = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} onInspect={onInspect} />)

    const firstRow = screen.getByText('Show.S01E01.mkv').closest('tr')!
    await user.click(within(firstRow).getByRole('button', { name: 'Inspect' }))

    expect(onInspect).toHaveBeenCalledTimes(1)
    expect(onInspect).toHaveBeenCalledWith(1)
  })

  it('changing the page size fires onPageSizeChange', async () => {
    const user = userEvent.setup()
    const onPageSizeChange = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} onPageSizeChange={onPageSizeChange} />)

    await user.selectOptions(screen.getByLabelText('Page size'), '100')

    expect(onPageSizeChange).toHaveBeenCalledWith(100)
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

  it('clicking the Updated header toggles sort direction', async () => {
    const user = userEvent.setup()
    const onSortDirChange = vi.fn()
    render(<QueueTable page={queuePageFixture} {...defaultProps} sortDir="desc" onSortDirChange={onSortDirChange} />)

    await user.click(screen.getByRole('button', { name: /Updated/ }))

    expect(onSortDirChange).toHaveBeenCalledWith('asc')
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
