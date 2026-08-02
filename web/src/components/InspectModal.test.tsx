import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobStatus } from '../api/types'
import InspectModal from './InspectModal'
import { jobDetailDupeFixture, jobDetailFixture, jobDetailHumanIdentFixture } from './testFixtures'

const { getJobMock } = vi.hoisted(() => ({ getJobMock: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getJob: getJobMock }
})

describe('InspectModal', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders "Labelled as" with SxxEyy derived from the claimed plugin candidate, path, and instance', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Labelled as')).closest('section')!
    expect(within(section).getByText('S01E01 · main')).toBeInTheDocument()
    expect(within(section).getByText(jobDetailFixture.file.sonarr_path)).toBeInTheDocument()
  })

  it('renders humanized plugin status chips and a percentage candidate chip', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/whisper-transcript/)).toBeInTheDocument()
    expect(screen.getByText('found evidence')).toBeInTheDocument()
    expect(screen.getByText(/ocr-subs/)).toBeInTheDocument()
    expect(screen.getByText('skipped: no subtitle track')).toBeInTheDocument()
    expect(screen.getByText('S01E01 — 92%')).toBeInTheDocument()
  })

  it('never renders raw internal terms (in_series/s_claimed/s_alt)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    const { container } = render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/\bin_series\b/)
    expect(text).not.toMatch(/\bs_claimed\b/)
    expect(text).not.toMatch(/\bs_alt\b/)
  })

  it('renders the Confidence section as sentences', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Confidence it is the labelled episode: 92%')).toBeInTheDocument()
  })

  it('"Identified as" names the in-series remap target with its confidence', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 0.1, ident: { series: 'claimed', season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} },
            { confidence: 0.97, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} },
          ],
          normalized: [
            { kind: 'in_series', episode_ids: [100] },
            { kind: 'in_series', episode_ids: [999] },
          ],
        },
      ],
      verdict: {
        ...jobDetailFixture.verdict!,
        s_claimed: 0.1,
        s_alt: 0.97,
        proposed_action: { kind: 'remap', target_episode_ids: [999] },
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Identified as')).closest('section')!
    expect(within(section).getByText(/S01E05/)).toBeInTheDocument()
    expect(within(section).getByText(/97%/)).toBeInTheDocument()
  })

  it('"Identified as" reports a cross-series match with a TVDB link', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 0.85, ident: { series: { tvdb: 81189 }, season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} },
          ],
          normalized: [{ kind: 'cross_series', external_ids: { tvdb: 81189 } }],
        },
      ],
      verdict: {
        ...jobDetailFixture.verdict!,
        s_claimed: 0.05,
        s_alt: 0.85,
        proposed_action: { kind: 'replace' },
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Identified as')).closest('section')!
    expect(within(section).getByText(/different series/)).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: 'TVDB' })).toHaveAttribute(
      'href',
      'https://thetvdb.com/dereferrer/series/81189',
    )
  })

  it('"Identified as" reports junk when replace is proposed with no cross-series evidence', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.05, ident: null, numbering: null, evidence: {} }],
          normalized: [{ kind: 'junk' }],
        },
      ],
      verdict: {
        ...jobDetailFixture.verdict!,
        s_claimed: 0.02,
        s_alt: 0.05,
        proposed_action: { kind: 'replace' },
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText("Content didn't match any episode.")).toBeInTheDocument()
  })

  it('"Identified as" shows the human override for an is_other verdict', async () => {
    getJobMock.mockResolvedValue(jobDetailHumanIdentFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Identified as')).closest('section')!
    expect(within(section).getByText(/S02E03E04/)).toBeInTheDocument()
    expect(within(section).getByText(/human override/)).toBeInTheDocument()
  })

  it.each([
    ['matched' as const, 'matched' as JobStatus, false, 'Verified match.'],
    ['quarantine' as const, 'quarantine' as JobStatus, false, 'Waiting for human review.'],
    ['inconclusive' as const, 'inconclusive' as JobStatus, false, 'Not enough evidence to judge.'],
    ['remediate' as const, 'remediated' as JobStatus, false, 'Fix applied.'],
    ['remediate' as const, 'remediated' as JobStatus, true, 'Fix applied (dry run).'],
    ['remediate' as const, 'quarantine' as JobStatus, false, 'Fix attempted, needs review.'],
    ['remediate' as const, 'error' as JobStatus, false, 'Error while applying the fix.'],
  ])('plain-language outcome: outcome=%s status=%s dryRun=%s -> %s', async (outcome, jobStatus, dryRun, expected) => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      job: { ...jobDetailFixture.job, status: jobStatus },
      verdict: { ...jobDetailFixture.verdict!, outcome },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} dryRun={dryRun} />)

    expect(await screen.findByText(expected)).toBeInTheDocument()
  })

  it('shows a dupe warning sentence when verdict.dupe_info is present', async () => {
    getJobMock.mockResolvedValue(jobDetailDupeFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(
      await screen.findByText('Visually near-identical to Other.S01E01.mkv (similarity 93%)'),
    ).toBeInTheDocument()
  })

  it('omits the dupe section when dupe_info is absent', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByText('Possible duplicate')).not.toBeInTheDocument()
  })

  it('shows the series title in the header when external_ids.title is present', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: /Test Show/ })).toBeInTheDocument()
    expect(screen.queryByText('Series 10')).not.toBeInTheDocument()
  })

  it('falls back to "Series {id}" in the header when external_ids is null', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, external_ids: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: 'Series 10' })).toBeInTheDocument()
  })

  it('falls back to "Series {id}" in the header when external_ids.title is null', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, external_ids: { ...jobDetailFixture.external_ids!, title: null } })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: /^Series 10/ })).toBeInTheDocument()
  })

  it('shows a TVDB link in the header when external_ids are present', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const link = await screen.findByRole('link', { name: 'TVDB' })
    expect(link).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/series/81189')
    expect(screen.getByRole('link', { name: 'IMDB' })).toHaveAttribute(
      'href',
      'https://www.imdb.com/title/tt0903747/',
    )
  })

  it('omits TVDB/IMDB links in the header when external_ids is null', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, external_ids: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByRole('link', { name: 'TVDB' })).not.toBeInTheDocument()
  })

  it('renders framegrab timestamp badges from asset tool_meta.timestamp_s', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const frames = await screen.findAllByAltText(/frame \d/)
    expect(frames).toHaveLength(2)
    expect(frames[0]).toHaveAttribute('src', '/api/v1/jobs/42/assets/2')
    expect(frames[0]).toHaveAttribute('loading', 'lazy')
    expect(frames[1]).toHaveAttribute('src', '/api/v1/jobs/42/assets/3')
    expect(screen.getByText('1:05')).toBeInTheDocument()
    expect(screen.getByText('2:10')).toBeInTheDocument()
  })

  it('renders the transcript excerpt', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/Hello world/)).toBeInTheDocument()
    expect(screen.getByText(/Second line/)).toBeInTheDocument()
  })

  it('shows the remediation log when present', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/interruption_guard/)).toBeInTheDocument()
    expect(screen.getByText(/no interruption in progress/)).toBeInTheDocument()
  })

  it('omits the remediation log section when absent', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, remediation_log: null },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByText('Remediation log')).not.toBeInTheDocument()
  })

  it('renders a fallback row instead of throwing when candidates are malformed', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'flaky-plugin',
          version: '0.1.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 'not-a-number', ident: null, numbering: null }, // malformed: confidence not numeric
            null, // malformed: not an object
            { confidence: 0.7, ident: null, numbering: 'tvdb', evidence: {} }, // valid, but no matching normalized entry
          ],
          normalized: [],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/flaky-plugin/)).toBeInTheDocument()
    expect(screen.getAllByText('unrecognized entry')).toHaveLength(2)
    expect(screen.getByText('confidence 70%')).toBeInTheDocument()
  })

  it('renders a Close button that calls onClose', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue(jobDetailFixture)
    const onClose = vi.fn()
    render(<InspectModal jobId={42} open onClose={onClose} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    await user.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).toHaveBeenCalled()
  })
})
