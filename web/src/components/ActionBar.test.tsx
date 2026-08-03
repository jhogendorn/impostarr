import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import ActionBar from './ActionBar'
import { jobDetailFixture, jobDetailHumanIdentFixture } from './testFixtures'

const { approveJobMock, rejectJobMock, postVerdictMock, replaceJobMock, rerunJobMock } = vi.hoisted(() => ({
  approveJobMock: vi.fn(),
  rejectJobMock: vi.fn(),
  postVerdictMock: vi.fn(),
  replaceJobMock: vi.fn(),
  rerunJobMock: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    approveJob: approveJobMock,
    rejectJob: rejectJobMock,
    postVerdict: postVerdictMock,
    replaceJob: replaceJobMock,
    rerunJob: rerunJobMock,
    parkJob: vi.fn(),
    unparkJob: vi.fn(),
  }
})

describe('ActionBar', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders the six main controls in order: Mark Correct, Apply Remap, Trash and Regrab, Dismiss, Ignore Mismatch, Reidentify', () => {
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    const labels = [
      screen.getByRole('button', { name: 'Mark Correct' }),
      screen.getByRole('combobox', { name: 'Apply Remap' }),
      screen.getByRole('button', { name: 'Trash and Regrab' }),
      screen.getByRole('button', { name: 'Dismiss' }),
      screen.getByRole('button', { name: 'Ignore Mismatch' }),
      screen.getByRole('button', { name: 'Reidentify' }),
    ]
    const positions = labels.map((el) => el.compareDocumentPosition(document.body))
    expect(positions.every((p) => (p & Node.DOCUMENT_POSITION_CONTAINS) !== 0)).toBe(true)
  })

  it('Mark Correct posts an is_claimed verdict', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'matched', verdict_id: 1, proposed_remap: null })
    const onChanged = vi.fn()
    render(<ActionBar job={jobDetailFixture} onChanged={onChanged} />)

    await user.click(screen.getByRole('button', { name: 'Mark Correct' }))

    expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'is_claimed' })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('Trash and Regrab is available with no proposed_action at all (always available)', async () => {
    const user = userEvent.setup()
    replaceJobMock.mockResolvedValue({ result: 'remediated' })
    render(
      <ActionBar
        job={{ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } }}
        onChanged={vi.fn()}
      />,
    )

    const button = screen.getByRole('button', { name: 'Trash and Regrab' })
    expect(button).toBeEnabled()
    await user.click(button)

    expect(replaceJobMock).toHaveBeenCalledWith(42)
  })

  it('Trash and Regrab is available even when the current proposal is a remap (not gated on proposed_action.kind)', async () => {
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Trash and Regrab' })).toBeEnabled()
  })

  it('Apply Remap dropdown offers a Candidates group (descending confidence) and an All Episodes group, each "SxxEyy - Title"', () => {
    render(
      <ActionBar
        job={{
          ...jobDetailFixture,
          series_episodes: [
            { id: 100, season: 1, episode: 1, title: 'Pilot' },
            { id: 999, season: 1, episode: 5, title: 'Fifth' },
          ],
          plugin_results: [
            {
              name: 'whisper-transcript',
              version: '1.0.0',
              status: 'ok',
              reason: null,
              candidates: [{ confidence: 0.7, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} }],
              normalized: [{ kind: 'in_series', episode_ids: [999] }],
            },
          ],
          episode_labels: { ...jobDetailFixture.episode_labels, 999: { id: 999, season: 1, episode: 5, title: 'Fifth' } },
        }}
        onChanged={vi.fn()}
      />,
    )

    const select = screen.getByRole('combobox', { name: 'Apply Remap' })
    const candidatesGroup = within(select).getByRole('group', { name: 'Candidates' })
    expect(within(candidatesGroup).getByRole('option', { name: 'S01E05 - Fifth' })).toBeInTheDocument()
    const allGroup = within(select).getByRole('group', { name: 'All Episodes' })
    expect(within(allGroup).getByRole('option', { name: 'S01E01 - Pilot' })).toBeInTheDocument()
    expect(within(allGroup).getByRole('option', { name: 'S01E05 - Fifth' })).toBeInTheDocument()
  })

  it('selecting an Apply Remap option submits an is_other verdict then approves (human_ident path), immediately', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'quarantine', verdict_id: 5, proposed_remap: { kind: 'remap', target_episode_ids: [999] } })
    approveJobMock.mockResolvedValue({ result: 'remediated' })
    const onChanged = vi.fn()
    render(
      <ActionBar
        job={{
          ...jobDetailFixture,
          series_episodes: [
            { id: 100, season: 1, episode: 1, title: 'Pilot' },
            { id: 999, season: 1, episode: 5, title: 'Fifth' },
          ],
        }}
        onChanged={onChanged}
      />,
    )

    const select = screen.getByRole('combobox', { name: 'Apply Remap' })
    await user.selectOptions(select, 'S01E05 - Fifth')

    await waitFor(() => expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'is_other', ident: { season: 1, episodes: [5] } }))
    await waitFor(() => expect(approveJobMock).toHaveBeenCalledWith(42))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('Dismiss only renders when a proposal exists', () => {
    const { rerender } = render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()

    rerender(<ActionBar job={{ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } }} onChanged={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument()
  })

  it('Dismiss calls reject', async () => {
    const user = userEvent.setup()
    rejectJobMock.mockResolvedValue({ result: 'quarantine' })
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(rejectJobMock).toHaveBeenCalledWith(42)
  })

  it('Ignore Mismatch posts an ignore verdict', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'inconclusive', verdict_id: 1, proposed_remap: null })
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Ignore Mismatch' }))
    expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'ignore' })
  })

  it('Reidentify calls the (unrenamed) rerun endpoint via rerunJob', async () => {
    const user = userEvent.setup()
    rerunJobMock.mockResolvedValue({ result: 'pending' })
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Reidentify' }))
    expect(rerunJobMock).toHaveBeenCalledWith(42)
  })

  it('omits Reidentify for a remediated job (no remediated->pending transition exists)', () => {
    render(<ActionBar job={{ ...jobDetailFixture, job: { ...jobDetailFixture.job, status: 'remediated' } }} onChanged={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Reidentify' })).not.toBeInTheDocument()
  })

  it('works from a human is_other verdict (human_ident, no proposed_action) — Apply Remap still available, Dismiss hidden (no proposed_action)', () => {
    render(<ActionBar job={jobDetailHumanIdentFixture} onChanged={vi.fn()} />)
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toBeInTheDocument()
  })

  it('renders the ApiError message inline on failure', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockRejectedValue(new ApiError(409, { detail: 'job is not quarantine' }))
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Ignore Mismatch' }))

    expect(await screen.findByText('409: job is not quarantine')).toBeInTheDocument()
  })

  it('color-codes Mark Correct (green), Apply Remap (indigo), Trash and Regrab (red), Reidentify (neutral slate)', () => {
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Mark Correct' })).toHaveClass('border-emerald-600/50')
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toHaveClass('border-indigo-600/50')
    expect(screen.getByRole('button', { name: 'Trash and Regrab' })).toHaveClass('border-red-600/50')
    expect(screen.getByRole('button', { name: 'Reidentify' })).toHaveClass('border-slate-700')
  })

  it('a single shared explainer overlay (not one per button) shows the hovered control\'s explainer text, absolutely positioned (overlay, not reflow)', async () => {
    const user = userEvent.setup()
    render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} />)

    const overlay = screen.getByRole('tooltip', { hidden: true })
    expect(overlay).toHaveClass('absolute')
    expect(overlay).not.toHaveTextContent(/Marks this file as verified/)

    await user.hover(screen.getByRole('button', { name: 'Mark Correct' }))
    expect(overlay).toHaveTextContent(/Marks this file as verified/)

    await user.hover(screen.getByRole('button', { name: 'Trash and Regrab' }))
    expect(overlay).toHaveTextContent(/Moves the current file to Trash/)
    expect(overlay).not.toHaveTextContent(/Marks this file as verified/)
  })
})
