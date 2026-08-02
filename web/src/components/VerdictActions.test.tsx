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

  it('shows Approve/Reject for quarantine with a proposed action, and disables while approving', async () => {
    const user = userEvent.setup()
    let resolveApprove!: (value: { result: 'matched' }) => void
    approveJobMock.mockReturnValue(
      new Promise((resolve) => {
        resolveApprove = resolve
      }),
    )
    const onChanged = vi.fn()
    render(<VerdictActions job={jobDetailFixture} onChanged={onChanged} />)

    const approveButton = screen.getByRole('button', { name: 'Approve action' })
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(screen.getByText('Proposed: remap → episodes 100')).toBeInTheDocument()

    await user.click(approveButton)

    expect(approveJobMock).toHaveBeenCalledWith(42)
    expect(approveButton).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()

    resolveApprove({ result: 'matched' })
    await waitFor(() => expect(approveButton).not.toBeDisabled())
    expect(onChanged).toHaveBeenCalled()
  })

  it('shows Approve/Reject for a human is_other verdict (human_ident, no proposed_action)', async () => {
    const user = userEvent.setup()
    approveJobMock.mockResolvedValue({ result: 'matched' })
    render(<VerdictActions job={jobDetailHumanIdentFixture} onChanged={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Approve action' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(screen.getByText('Proposed: remap → S2E3,4')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Approve action' }))

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

  it('is_other form submits parsed season/episodes', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'quarantine', verdict_id: 1, proposed_remap: null })
    render(<VerdictActions job={jobDetailFixture} onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Is other…' }))
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

    await user.click(screen.getByRole('button', { name: 'Ignore' }))

    expect(await screen.findByText('409: job is not quarantine')).toBeInTheDocument()
  })
})
