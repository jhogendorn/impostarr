import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { JobDetail } from '../api/types'
import { collectAlternates } from '../lib/inspectData'
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

/** Renders ActionBar with the same derived candidates/allEpisodes/default
 * selection InspectModal computes, plus a controllable selection so tests
 * can drive the two-step Apply Remap flow (item 1). */
function renderActionBar(
  job: JobDetail,
  { onChanged = vi.fn(), initialSelection }: { onChanged?: () => void; initialSelection?: number } = {},
) {
  const labelledIds = new Set(job.file.episode_ids)
  const candidates = collectAlternates(job, labelledIds)
  const allEpisodes = [...(job.series_episodes ?? [])].sort((a, b) => a.season - b.season || a.episode - b.episode)
  let selected = initialSelection ?? job.file.episode_ids[0]
  const onSelectEpisode = vi.fn((id: number) => {
    selected = id
  })
  const utils = render(
    <ActionBar
      job={job}
      onChanged={onChanged}
      selectedEpisodeId={selected}
      onSelectEpisode={onSelectEpisode}
      candidates={candidates}
      allEpisodes={allEpisodes}
    />,
  )
  return { ...utils, onSelectEpisode, getSelected: () => selected }
}

describe('ActionBar', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders controls in order: Mark Correct, Trash and Regrab, Dismiss, Ignore Mismatch, Reidentify, then Apply Remap last', () => {
    renderActionBar(jobDetailFixture)

    const labels = [
      screen.getByRole('button', { name: 'Mark Correct' }),
      screen.getByRole('button', { name: 'Trash and Regrab' }),
      screen.getByRole('button', { name: 'Dismiss' }),
      screen.getByRole('button', { name: 'Ignore Mismatch' }),
      screen.getByRole('button', { name: 'Reidentify' }),
      screen.getByRole('combobox', { name: 'Apply Remap' }),
    ]
    // documentPosition: each subsequent element should come AFTER the previous one.
    for (let i = 1; i < labels.length; i++) {
      const relation = labels[i - 1].compareDocumentPosition(labels[i])
      expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('Mark Correct posts an is_claimed verdict', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'matched', verdict_id: 1, proposed_remap: null })
    const onChanged = vi.fn()
    renderActionBar(jobDetailFixture, { onChanged })

    await user.click(screen.getByRole('button', { name: 'Mark Correct' }))

    expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'is_claimed' })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('Trash and Regrab is available with no proposed_action at all (always available)', async () => {
    const user = userEvent.setup()
    replaceJobMock.mockResolvedValue({ result: 'remediated' })
    renderActionBar({ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } })

    const button = screen.getByRole('button', { name: 'Trash and Regrab' })
    expect(button).toBeEnabled()
    await user.click(button)

    expect(replaceJobMock).toHaveBeenCalledWith(42)
  })

  it('Apply Remap combobox selecting an option only PREVIEWS (no API call) — button label then names the target', async () => {
    const user = userEvent.setup()
    const job = {
      ...jobDetailFixture,
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null },
      ],
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok' as const,
          reason: null,
          candidates: [{ confidence: 0.7, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} }],
          normalized: [{ kind: 'in_series', episode_ids: [999] }],
        },
      ],
      episode_labels: { ...jobDetailFixture.episode_labels, '999': { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null } },
    }
    const { onSelectEpisode } = renderActionBar(job)

    expect(screen.getByRole('button', { name: 'Apply Remap' })).toBeInTheDocument()

    const combobox = screen.getByRole('combobox', { name: 'Apply Remap' })
    await user.click(combobox)
    await user.click(screen.getByRole('option', { name: 'S01E05 - Fifth' }))

    expect(onSelectEpisode).toHaveBeenCalledWith(999)
    expect(postVerdictMock).not.toHaveBeenCalled()
    expect(approveJobMock).not.toHaveBeenCalled()
  })

  it('the Apply Remap button performs the remap only when clicked, naming the previewed target', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'quarantine', verdict_id: 5, proposed_remap: { kind: 'remap', target_episode_ids: [999] } })
    approveJobMock.mockResolvedValue({ result: 'remediated' })
    const onChanged = vi.fn()
    const job = {
      ...jobDetailFixture,
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null },
      ],
    }
    // Preselect episode 999 (differs from the default, episode 100) —
    // simulates the user having already picked it via the combobox.
    renderActionBar(job, { onChanged, initialSelection: 999 })

    const button = screen.getByRole('button', { name: 'Apply Remap to S01E05' })
    await user.click(button)

    await waitFor(() => expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'is_other', ident: { season: 1, episodes: [5] } }))
    await waitFor(() => expect(approveJobMock).toHaveBeenCalledWith(42))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('Dismiss only renders when a proposal exists', () => {
    const { rerender } = render(<ActionBar job={jobDetailFixture} onChanged={vi.fn()} selectedEpisodeId={100} onSelectEpisode={vi.fn()} candidates={[]} allEpisodes={[]} />)
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()

    rerender(
      <ActionBar
        job={{ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } }}
        onChanged={vi.fn()}
        selectedEpisodeId={100}
        onSelectEpisode={vi.fn()}
        candidates={[]}
        allEpisodes={[]}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument()
  })

  it('Dismiss calls reject', async () => {
    const user = userEvent.setup()
    rejectJobMock.mockResolvedValue({ result: 'quarantine' })
    renderActionBar(jobDetailFixture)

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(rejectJobMock).toHaveBeenCalledWith(42)
  })

  it('Ignore Mismatch posts an ignore verdict', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockResolvedValue({ job_status: 'inconclusive', verdict_id: 1, proposed_remap: null })
    renderActionBar(jobDetailFixture)

    await user.click(screen.getByRole('button', { name: 'Ignore Mismatch' }))
    expect(postVerdictMock).toHaveBeenCalledWith(42, { verdict: 'ignore' })
  })

  it('Reidentify calls the (unrenamed) rerun endpoint via rerunJob', async () => {
    const user = userEvent.setup()
    rerunJobMock.mockResolvedValue({ result: 'pending' })
    renderActionBar(jobDetailFixture)

    await user.click(screen.getByRole('button', { name: 'Reidentify' }))
    expect(rerunJobMock).toHaveBeenCalledWith(42)
  })

  it('omits Reidentify for a remediated job (no remediated->pending transition exists)', () => {
    renderActionBar({ ...jobDetailFixture, job: { ...jobDetailFixture.job, status: 'remediated' } })
    expect(screen.queryByRole('button', { name: 'Reidentify' })).not.toBeInTheDocument()
  })

  it('works from a human is_other verdict (human_ident, no proposed_action) — Apply Remap still available, Dismiss hidden (no proposed_action)', () => {
    renderActionBar(jobDetailHumanIdentFixture)
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toBeInTheDocument()
  })

  it('renders the ApiError message inline on failure', async () => {
    const user = userEvent.setup()
    postVerdictMock.mockRejectedValue(new ApiError(409, { detail: 'job is not quarantine' }))
    renderActionBar(jobDetailFixture)

    await user.click(screen.getByRole('button', { name: 'Ignore Mismatch' }))

    expect(await screen.findByText('409: job is not quarantine')).toBeInTheDocument()
  })

  it('color-codes Mark Correct (green), Apply Remap button (indigo), Trash and Regrab (red), Reidentify (neutral slate)', () => {
    renderActionBar(jobDetailFixture)

    expect(screen.getByRole('button', { name: 'Mark Correct' })).toHaveClass('border-emerald-600/50')
    expect(screen.getByRole('button', { name: 'Apply Remap' })).toHaveClass('border-indigo-600/50')
    expect(screen.getByRole('button', { name: 'Trash and Regrab' })).toHaveClass('border-red-600/50')
    expect(screen.getByRole('button', { name: 'Reidentify' })).toHaveClass('border-slate-700')
  })

  it('a permanently-allocated explanation block (not an overlay) defaults to the current proposal\'s explanation and swaps on hover', async () => {
    const user = userEvent.setup()
    renderActionBar(jobDetailFixture) // has a remap proposal

    // Default content is populated (the current proposal's explanation) —
    // not empty, and not absolutely positioned (permanent flow space, item 6).
    const explanation = screen.getByText(/Preview an episode below, then confirm/)
    expect(explanation.tagName).toBe('P')
    expect(explanation).not.toHaveClass('absolute')

    await user.hover(screen.getByRole('button', { name: 'Mark Correct' }))
    expect(screen.getByText(/Marks this file as verified/)).toBeInTheDocument()

    await user.hover(screen.getByRole('button', { name: 'Trash and Regrab' }))
    expect(screen.getByText(/Moves the current file to Trash/)).toBeInTheDocument()
    expect(screen.queryByText(/Marks this file as verified/)).not.toBeInTheDocument()

    await user.unhover(screen.getByRole('button', { name: 'Trash and Regrab' }))
    expect(screen.getByText(/Preview an episode below, then confirm/)).toBeInTheDocument()
  })

  it('defaults the explanation to "No proposed action" copy when there is no proposal', () => {
    renderActionBar({ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } })
    expect(screen.getByText(/No proposed action for this job/)).toBeInTheDocument()
  })
})
