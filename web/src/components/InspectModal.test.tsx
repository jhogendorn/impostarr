import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { JobStatus } from '../api/types'
import InspectModal from './InspectModal'
import {
  jobDetailDupeFixture,
  jobDetailFixture,
  jobDetailHumanIdentFixture,
  jobDetailMatchedFixture,
} from './testFixtures'

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

  it('renders the restored plugin-results table (name/version, raw status, raw reason, percent candidate confidence)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    expect(within(section).getByText(/whisper-transcript/)).toBeInTheDocument()
    expect(within(section).getByText('ok')).toBeInTheDocument()
    expect(within(section).getByText(/ocr-subs/)).toBeInTheDocument()
    expect(within(section).getByText('abstain')).toBeInTheDocument()
    expect(within(section).getByText('no subtitle track')).toBeInTheDocument()
    expect(within(section).getByText(/conf 92% · tvdb S1E1/)).toBeInTheDocument()
  })

  it('the plugin-results table translates in_series/cross_series/junk tokens to words and carries an evidence tooltip', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    expect(within(section).getByText(/matches this series: episodes 100/)).toBeInTheDocument()
    const candidateLine = within(section).getByText(/conf 92% · tvdb S1E1/)
    expect(candidateLine).toHaveAttribute('title', JSON.stringify({}))
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

  it('a matched job gets ONE "Verified as ... confidence" statement, with the redundant plain outcome line dropped', async () => {
    getJobMock.mockResolvedValue(jobDetailMatchedFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Verified as S01E01 — 97% confidence.')).toBeInTheDocument()
    expect(screen.queryByText('Verified match.')).not.toBeInTheDocument()
  })

  it('a genuinely inconclusive job (no usable evidence) gets one statement and no redundant outcome line', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [],
      verdict: {
        s_claimed: null,
        s_alt: null,
        outcome: 'inconclusive',
        proposed_action: null,
        remediation_log: null,
        source: 'auto',
        human_ident: null,
        dupe_info: null,
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Could not be identified — no usable evidence.')).toBeInTheDocument()
    expect(screen.queryByText('Not enough evidence to judge.')).not.toBeInTheDocument()
  })

  it('a mid-band quarantine job with a credible remap alternative shows the mislabel statement AND keeps the review-workflow line (adds new info)', async () => {
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

    const section = (await screen.findByText('Identification')).closest('section')!
    expect(
      within(section).getByText('This file appears to be S01E05 (97% confidence), not the labelled S01E01 (10%).'),
    ).toBeInTheDocument()
    expect(within(section).getByText('Waiting for human review.')).toBeInTheDocument()
  })

  it('a mislabel identification statement with no matching plugin candidate falls back to generic wording', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: {
        ...jobDetailFixture.verdict!,
        s_claimed: 0.1,
        s_alt: 0.97,
        proposed_action: { kind: 'remap', target_episode_ids: [999999] }, // no plugin candidate resolves to this
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(
      await screen.findByText('This file appears to be a different episode (97% confidence), not the labelled S01E01 (10%).'),
    ).toBeInTheDocument()
  })

  it('Identification reports a cross-series match with a TVDB link', async () => {
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

    const section = (await screen.findByText('Identification')).closest('section')!
    expect(
      within(section).getByText(/This file appears to be a different series entirely \(85% confidence\)/),
    ).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: 'TVDB' })).toHaveAttribute(
      'href',
      'https://thetvdb.com/dereferrer/series/81189',
    )
  })

  it('Identification reports junk when replace is proposed with no cross-series evidence', async () => {
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

    expect(
      await screen.findByText("This file's content didn't match any known episode (5% confidence)."),
    ).toBeInTheDocument()
  })

  it('Identification shows the human override for an is_other verdict', async () => {
    getJobMock.mockResolvedValue(jobDetailHumanIdentFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Identification')).closest('section')!
    expect(within(section).getByText(/S02E03E04/)).toBeInTheDocument()
    expect(within(section).getByText(/human override/)).toBeInTheDocument()
  })

  // matched/quarantine/inconclusive plain outcome lines are covered by the
  // dedicated statement tests above (where the redundancy-drop rule is the
  // point); `remediate` always keeps its outcome line regardless of the
  // identification statement, since it conveys new info (an action taken).
  it.each([
    ['remediate' as const, 'remediated' as JobStatus, false, 'Fix applied.'],
    ['remediate' as const, 'remediated' as JobStatus, true, 'Fix applied (dry run).'],
    ['remediate' as const, 'quarantine' as JobStatus, false, 'Fix attempted, needs review.'],
    ['remediate' as const, 'error' as JobStatus, false, 'Error while applying the fix.'],
  ])('plain-language outcome (always kept for remediate): outcome=%s status=%s dryRun=%s -> %s', async (outcome, jobStatus, dryRun, expected) => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      job: { ...jobDetailFixture.job, status: jobStatus },
      verdict: { ...jobDetailFixture.verdict!, outcome },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} dryRun={dryRun} />)

    expect(await screen.findByText(expected)).toBeInTheDocument()
  })

  it('the Fingerprint section shows perceptual-hash and corpus-storage info when present', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    expect(within(section).getByText('Perceptual hash: 16 frames (phash v1) · stored in corpus (auto, 97%)')).toBeInTheDocument()
  })

  it('the Fingerprint section omits the corpus clause when there is no corpus entry', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, phash_corpus: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    expect(within(section).getByText('Perceptual hash: 16 frames (phash v1)')).toBeInTheDocument()
    expect(within(section).queryByText(/stored in corpus/)).not.toBeInTheDocument()
  })

  it('the Fingerprint section also carries the dupe warning when dupe_info is present', async () => {
    getJobMock.mockResolvedValue(jobDetailDupeFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    expect(within(section).getByText('Visually near-identical to Other.S01E01.mkv (similarity 93%)')).toBeInTheDocument()
  })

  it('omits the Fingerprint section entirely when there is neither a frame hash nor a dupe hit', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, frame_hash: null, phash_corpus: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByText('Fingerprint')).not.toBeInTheDocument()
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
            { confidence: 0.7, ident: null, numbering: 'tvdb', evidence: {} }, // valid, ident-less candidate
          ],
          normalized: [],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/flaky-plugin/)).toBeInTheDocument()
    expect(screen.getAllByText('unrecognized entry')).toHaveLength(2)
    expect(screen.getByText(/conf 70% · tvdb/)).toBeInTheDocument()
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
