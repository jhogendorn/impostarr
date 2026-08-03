import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TrashTable from './TrashTable'
import { trashPageFixture } from './testFixtures'

const { getTrashMock, deleteTrashItemMock, restoreTrashItemMock } = vi.hoisted(() => ({
  getTrashMock: vi.fn(),
  deleteTrashItemMock: vi.fn(),
  restoreTrashItemMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  getTrash: getTrashMock,
  deleteTrashItem: deleteTrashItemMock,
  restoreTrashItem: restoreTrashItemMock,
}))

describe('TrashTable', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    getTrashMock.mockResolvedValue(trashPageFixture)
    deleteTrashItemMock.mockResolvedValue({ result: 'deleted' })
    restoreTrashItemMock.mockResolvedValue({ result: 'restored', original_path: '/x', note: '' })
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('renders trash rows with file, instance, resolved series title + episode label, size, and an expires-in countdown (item 17: never raw ids when a resolved title/label is available)', async () => {
    render(<TrashTable />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(screen.getByText('Show.S01E01.mkv')).toBeInTheDocument()
    // item 1 resolves fully: title + label, not raw ids.
    expect(screen.getByText('Show · S01E01')).toBeInTheDocument()
    // item 2: resolution failed (series_title/episode_label both null) ->
    // falls back to the raw ids, exactly like pre-item-17 rendering.
    expect(screen.getByText('Series 11 · 200, 201')).toBeInTheDocument()
    expect(screen.getAllByText('main')).toHaveLength(2)
    // item 1: expires_in_s = 1209600 (14d) -> "14d 00h"
    expect(screen.getByText('14d 00h')).toBeInTheDocument()
    // item 2: expires_in_s = -3600 (already past) -> "expired"
    expect(screen.getByText('expired')).toBeInTheDocument()
  })

  it('ticks the countdown down as time passes', async () => {
    render(<TrashTable />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('14d 00h')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3600_000)
    })

    expect(screen.getByText('13d 23h')).toBeInTheDocument()
  })

  it('Restore calls restoreTrashItem and refetches', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<TrashTable />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    getTrashMock.mockClear()

    await user.click(screen.getAllByRole('button', { name: 'Restore' })[0])

    expect(restoreTrashItemMock).toHaveBeenCalledWith(1)
    await waitFor(() => expect(getTrashMock).toHaveBeenCalled())
  })

  it('Delete now opens a confirm dialog and only deletes after confirming', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<TrashTable />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    await user.click(screen.getAllByRole('button', { name: 'Delete now' })[0])

    expect(screen.getByText(/permanently deletes/i)).toBeInTheDocument()
    expect(deleteTrashItemMock).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(deleteTrashItemMock).not.toHaveBeenCalled()

    await user.click(screen.getAllByRole('button', { name: 'Delete now' })[0])
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete now' }))

    expect(deleteTrashItemMock).toHaveBeenCalledWith(1)
  })
})
