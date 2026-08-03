import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import VerdictActions from './VerdictActions'
import { jobDetailFixture, jobDetailHumanIdentFixture } from './testFixtures'

const { approveJobMock, rejectJobMock, postVerdictMock } = vi.hoisted(() => ({
  approveJobMock: vi.fn(),
  rejectJobMock: vi.fn(),
  postVerdictMock: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    approveJob: approveJobMock,
    rejectJob: rejectJobMock,
    postVerdict: postVerdictMock,
    parkJob: vi.fn(),
    unparkJob: vi.fn(),
    rerunJob: vi.fn(),
  }
})

describe('VerdictActions', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows Apply/Dismiss for quarantine with a proposed action (verb + episode label reflect the remap target), disabling while applying', async () => {
    const user = userEvent.setup()
    let resolveApprove!: (value: { result: 'matched' }) => void
    approveJobMock.mockReturnValue(
      new Promise((resolve) => {
        resolveApprove = resolve
      }),
    )
    const onChanged = vi.fn()
    render(<VerdictActions job={jobDetailFixture} onChanged={onChanged} />)

    // jobDetailFixture: proposed_action targets episode id 100, resolved
    // via episode_labels to S01E01 — the button names it, not the raw id.
    const applyButton = screen.getByRole('button', { name: 'Apply remap to S01E01' })
    expect(screen.getByRole('button', { name: 'Dismiss proposal' })).toBeInTheDocument()
    expect(screen.getByText('Proposed: remap → S01E01')).toBeInTheDocument()

    await user.click(applyButton)

    expect(approveJobMock).toHaveBeenCalledWith(42)
    expect(applyButton).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Dismiss proposal' })).toBeDisabled()

    resolveApprove({ result: 'matched' })
    await waitFor(() => expect(applyButton).not.toBeDisabled())
    expect(onChanged).toHaveBeenCalled()
  })

  it('falls back to raw episode ids in the button/proposal text when episode_labels did not resolve them', async () => {
    render(
      <VerdictActions
        job={{ ...jobDetailFixture, episode_labels: {} }}
        onChanged={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: 'Apply remap to episode(s) 100' })).toBeInTheDocument()
    expect(screen.getByText('Proposed: remap → episode(s) 100')).toBeInTheDocument()
  })

  it('a proposed replace applies with a destructive tone and names the Trash affordance in its explainer', () => {
    render(
      <VerdictActions
        job={{ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: { kind: 'replace' } } }}
        onChanged={vi.fn()}
      />,
    )

    const applyButton = screen.getByRole('button', { name: 'Apply replace' })
    expect(applyButton).toHaveClass('border-red-600/50')
    expect(screen.getByText(/moves to Trash/)).toBeInTheDocument()
  })

  it('shows Apply/Dismiss for a human is_other verdict (human_ident, no proposed_action), zero-padded', async () => {
    const user = userEvent.setup()
    approveJobMock.mockResolvedValue({ result: 'matched' })
    render(<VerdictActions job={jobDetailHumanIdentFixture} onChanged={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Apply remap to S02E03E04' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Dismiss proposal' })).toBeInTheDocument()
    expect(screen.getByText('Proposed: remap → S02E03E04')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Apply remap to S02E03E04' }))

    expect(approveJobMock).toHaveBeenCalledWith(42)
  })

  it('omits Rerun for a remediated job (no remediated→pending transition exists)', () => {
    render(
      <VerdictActions
        job={{ ...jobDetailFixture, job: { ...jobDetailFixture.job, status: 'remediated' } }}
        onChanged={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: 'Rerun' })).not.toBeInTheDocument()
  })

  it('"Correct to different episode…" form submits parsed season/episodes', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'quarantine', verdict_id: 1, proposed_remap: null })
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Correct to different episode…' }))
    await user.type(screen.getByLabelText('Season'), '2')
    await user.type(screen.getByLabelText('Episodes (CSV)'), '3, 4, 5')
    await user.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() =>
      expect(postVerdictMock).toHaveBeenCalledWith(42, {
        verdict: 'is_other',
        ident: { season: 2, episodes: [3, 4, 5] },
      }),
    )
  })

  it('renders the ApiError message inline on failure', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockRejectedValue(new ApiError(409, { detail: 'job is not quarantine' }))
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Mark ignored' }))

    expect(await screen.findByText('409: job is not quarantine')).toBeInTheDocument()
  })

  it('color-codes Confirm (green-ish) and Mark ignored (neutral slate)', () => {
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Confirm labelled episode' })).toHaveClass('border-emerald-600/50')
    expect(screen.getByRole('button', { name: 'Mark ignored' })).toHaveClass('border-slate-700')
  })

  it('each action shows a hover/focus explainer that does not preallocate layout space (absolutely positioned)', () => {
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    const tooltip = screen.getByText(/Marks this file as verified/)
    expect(tooltip).toHaveAttribute('role', 'tooltip')
    expect(tooltip).toHaveClass('absolute')
    expect(tooltip).toHaveClass('opacity-0')
  })

  it("Dismiss proposal's explainer distinguishes it from Mark ignored (ignore-vs-reject semantics)", () => {
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    expect(screen.getByText(/Clears this proposed fix only.*stays in Quarantine/)).toBeInTheDocument()
    expect(screen.getByText(/Moves this job to Inconclusive.*Different from Dismiss proposal/)).toBeInTheDocument()
  })
})
