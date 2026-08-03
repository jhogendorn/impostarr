import { useState } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import EpisodeCombobox from './EpisodeCombobox'
import type { EpisodeLabel } from '../api/types'
import type { AlternateCandidate } from '../lib/inspectData'

const allEpisodes: EpisodeLabel[] = [
  { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: null },
  { id: 500, season: 5, episode: 8, title: 'Stan of Arabia', tvdb_id: null },
  { id: 501, season: 5, episode: 9, title: 'Stan Time', tvdb_id: null },
  { id: 400, season: 4, episode: 1, title: 'Fourth Season Opener', tvdb_id: null },
]
const episodeLabels: Record<string, EpisodeLabel> = Object.fromEntries(allEpisodes.map((ep) => [String(ep.id), ep]))
const candidates: AlternateCandidate[] = [{ episodeIds: [501], season: 5, episodes: [9], confidence: 0.91 }]

function Wrapper({ onChange }: { onChange: (id: number) => void }) {
  const [value, setValue] = useState(100)
  return (
    <EpisodeCombobox
      ariaLabel="Apply Remap"
      value={value}
      onChange={(id) => {
        setValue(id)
        onChange(id)
      }}
      candidates={candidates}
      allEpisodes={allEpisodes}
      episodeLabels={episodeLabels}
    />
  )
}

describe('EpisodeCombobox', () => {
  it('renders as a text input (no native select), showing the current selection', () => {
    render(<Wrapper onChange={vi.fn()} />)
    const input = screen.getByRole('combobox', { name: 'Apply Remap' })
    expect(input.tagName).toBe('INPUT')
    expect(document.querySelector('select')).not.toBeInTheDocument()
    expect(input).toHaveValue('S01E01 - Pilot')
  })

  it('opens on focus: Candidates group expanded, season groups collapsed by default', async () => {
    const user = userEvent.setup()
    render(<Wrapper onChange={vi.fn()} />)
    await user.click(screen.getByRole('combobox', { name: 'Apply Remap' }))

    const listbox = screen.getByRole('listbox')
    const candidatesGroup = within(listbox).getByRole('group', { name: 'Candidates' })
    expect(within(candidatesGroup).getByRole('option', { name: 'S05E09 - Stan Time' })).toBeInTheDocument()

    const season5 = within(listbox).getByRole('group', { name: 'Season 5' })
    expect(within(season5).queryByRole('option')).not.toBeInTheDocument()
    expect(within(season5).getByText('Season 5')).toBeInTheDocument()
  })

  it('clicking a season header expands it to show its options', async () => {
    const user = userEvent.setup()
    render(<Wrapper onChange={vi.fn()} />)
    await user.click(screen.getByRole('combobox', { name: 'Apply Remap' }))

    const season5 = within(screen.getByRole('listbox')).getByRole('group', { name: 'Season 5' })
    await user.click(within(season5).getByText('Season 5'))

    expect(within(season5).getByRole('option', { name: 'S05E08 - Stan of Arabia' })).toBeInTheDocument()
    expect(within(season5).getByRole('option', { name: 'S05E09 - Stan Time' })).toBeInTheDocument()
  })

  it('typing filters options and auto-expands a season group with a match', async () => {
    const user = userEvent.setup()
    render(<Wrapper onChange={vi.fn()} />)
    const input = screen.getByRole('combobox', { name: 'Apply Remap' })
    await user.click(input)
    await user.type(input, 'Fourth')

    const listbox = screen.getByRole('listbox')
    expect(within(listbox).getByRole('option', { name: 'S04E01 - Fourth Season Opener' })).toBeInTheDocument()
    expect(within(listbox).queryByRole('option', { name: /Pilot/ })).not.toBeInTheDocument()
    expect(within(listbox).queryByRole('group', { name: 'Season 5' })).not.toBeInTheDocument()
  })

  it('clicking an option calls onChange and closes the listbox', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Wrapper onChange={onChange} />)
    await user.click(screen.getByRole('combobox', { name: 'Apply Remap' }))
    await user.click(screen.getByRole('option', { name: 'S05E09 - Stan Time' }))

    expect(onChange).toHaveBeenCalledWith(501)
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toHaveValue('S05E09 - Stan Time')
  })

  it('keyboard: ArrowDown then Enter selects the first (candidate) option', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Wrapper onChange={onChange} />)
    const input = screen.getByRole('combobox', { name: 'Apply Remap' })
    await user.click(input)
    await user.keyboard('{ArrowDown}{Enter}')

    expect(onChange).toHaveBeenCalledWith(501)
  })

  it('Escape closes without changing the selection', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Wrapper onChange={onChange} />)
    const input = screen.getByRole('combobox', { name: 'Apply Remap' })
    await user.click(input)
    await user.keyboard('{Escape}')

    expect(onChange).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(input).toHaveValue('S01E01 - Pilot')
  })

  it('closes on outside click', async () => {
    const user = userEvent.setup()
    render(
      <div>
        <Wrapper onChange={vi.fn()} />
        <button type="button">outside</button>
      </div>,
    )
    await user.click(screen.getByRole('combobox', { name: 'Apply Remap' }))
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'outside' }))
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })
})
