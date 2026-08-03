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

  // -- section 1: identity header, always present, filename first --------

  it('shows the filename as the heading, with series/season-episode/instance as a subtitle line (identity always at top)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const heading = await screen.findByRole('heading', { name: 'Show.S01E01.mkv' })
    const subtitle = heading.nextElementSibling as HTMLElement
    expect(within(subtitle).getByText(/Test Show/)).toBeInTheDocument()
    expect(within(subtitle).getByText(/S01E01/)).toBeInTheDocument()
    expect(within(subtitle).getByText(/main/)).toBeInTheDocument()
  })

  it('shows a placeholder heading ("Job #id") before the job detail has loaded', () => {
    getJobMock.mockReturnValue(new Promise(() => {})) // never resolves
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(screen.getByRole('heading', { name: 'Job #42' })).toBeInTheDocument()
  })

  it('shows a TVDB/IMDB link next to the series name in the header', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const link = await screen.findByRole('link', { name: 'TVDB' })
    expect(link).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/series/81189')
    expect(screen.getByRole('link', { name: 'IMDB' })).toHaveAttribute('href', 'https://www.imdb.com/title/tt0903747/')
  })

  it('omits TVDB/IMDB header links when external_ids is null', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, external_ids: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByRole('link', { name: 'TVDB' })).not.toBeInTheDocument()
  })

  it('falls back to "Series {id}" in the header when external_ids is null', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, external_ids: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/Series 10/)).toBeInTheDocument()
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

  it('the dialog panel carries the elevated indigo glow treatment', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    const panel = document.body.querySelector('.max-w-7xl')
    expect(panel).toHaveClass('glow-elevated')
  })

  // -- section 6: action bar sticky at the top, alongside identity ---------

  it('the action bar renders inside the same sticky header block as the identity row (shares the top, not the footer)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const heading = await screen.findByRole('heading', { name: 'Show.S01E01.mkv' })
    const stickyHeader = heading.closest('.sticky')!
    expect(stickyHeader).toHaveClass('top-0')
    expect(within(stickyHeader as HTMLElement).getByRole('button', { name: /Apply remap/ })).toBeInTheDocument()
  })

  // -- section 2: comparison — as labelled / confidence / identified as ---

  it('"As labelled" shows season/episode/title resolved via episode_labels, plus the file\'s own framegrabs nested in that column', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const heading = await screen.findByText('As labelled')
    const column = heading.closest('div')!
    expect(within(column).getByText('S01E01')).toBeInTheDocument()
    expect(within(column).getByText('Pilot')).toBeInTheDocument()
    const frames = within(column).getAllByAltText(/frame \d/)
    expect(frames).toHaveLength(2)
    expect(frames[0]).toHaveAttribute('src', '/api/v1/jobs/42/assets/2')
  })

  // -- layout regression (jsdom can't render real CSS, but these pin the
  // structural choices the production layout bug depended on, so a future
  // refactor that reintroduces the broken pattern trips a red test) ------

  it('the modal panel is wide (~90vw, max-w-7xl) — not the old max-w-3xl that left no room for three columns', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    const panel = document.body.querySelector('[class*="max-w-"]')!
    expect(panel).toHaveClass('w-[90vw]', 'max-w-7xl')
  })

  it('the identity row and the three-way text row are SEPARATE grids, not one grid spanning both', async () => {
    // Root cause of the production layout collapse: a single grid spanning
    // both rows lets a <pre>'s long-line max-content (used to size a
    // content-based "auto" track) hijack the identity row's column widths
    // too, since CSS Grid sizes a column from every item placed in it
    // across all rows. Two separate grids make that impossible structurally.
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const asLabelled = await screen.findByText('As labelled')
    const identityGrid = asLabelled.closest('.grid')!
    const transcriptHeading = screen.getByText('Transcript')
    const textGrid = transcriptHeading.closest('.grid')!
    expect(identityGrid).not.toBe(textGrid)
    expect(identityGrid).not.toContainElement(transcriptHeading)
  })

  it('the three text-comparison columns use Tailwind grid-cols-3 (built-in minmax(0,1fr) per column), not a hand-rolled 1fr track', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const textGrid = (await screen.findByText('Transcript')).closest('.grid')!
    expect(textGrid).toHaveClass('grid-cols-3')
  })

  it('framegrabs render as a horizontal-scroll strip of fixed-width thumbnails, not flex-wrap (which stacked them into vertical slivers in a narrow column)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const frame = (await screen.findAllByAltText(/frame \d/))[0]
    expect(frame).toHaveClass('w-40')
    expect(frame).not.toHaveClass('w-auto')
    const strip = frame.closest('div')!.parentElement!
    expect(strip).toHaveClass('overflow-x-auto')
    expect(strip).not.toHaveClass('flex-wrap')
  })

  it('shows an aggregate confidence figure centered between the two columns', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('confidence')).toBeInTheDocument()
  })

  it('"Identified as" offers a dropdown when more than one credible in-series alternate exists, and resolves the selected one\'s title', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      episode_labels: {
        ...jobDetailFixture.episode_labels,
        999: { id: 999, season: 1, episode: 5, title: 'Fifth' },
        998: { id: 998, season: 1, episode: 6, title: 'Sixth' },
      },
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 0.1, ident: { series: 'claimed', season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} },
            { confidence: 0.7, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} },
            { confidence: 0.6, ident: { series: 'claimed', season: 1, episodes: [6] }, numbering: 'tvdb', evidence: {} },
          ],
          normalized: [
            { kind: 'in_series', episode_ids: [100] },
            { kind: 'in_series', episode_ids: [999] },
            { kind: 'in_series', episode_ids: [998] },
          ],
        },
      ],
      verdict: {
        ...jobDetailFixture.verdict!,
        s_claimed: 0.1,
        s_alt: 0.7,
        proposed_action: { kind: 'remap', target_episode_ids: [999] },
      },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const select = await screen.findByRole('combobox', { name: 'Alternate identification' })
    expect(within(select).getByRole('option', { name: 'S01E05 (70%)' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: 'S01E06 (60%)' })).toBeInTheDocument()

    const identifiedHeading = screen.getByText('Identified as')
    const column = identifiedHeading.closest('div')!
    expect(within(column).getByText('Fifth')).toBeInTheDocument() // the higher-confidence one is selected by default
  })

  it('"Identified as" shows a plain statement (no dropdown) when there is exactly one in-series alternate', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      episode_labels: { ...jobDetailFixture.episode_labels, 999: { id: 999, season: 1, episode: 5, title: 'Fifth' } },
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

    await screen.findByText('Fifth')
    expect(screen.queryByRole('combobox', { name: 'Alternate identification' })).not.toBeInTheDocument()
  })

  it('"Identified as" shows a cross-series statement with TVDB/IMDB links when the winner is a different series', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.85, ident: { series: { tvdb: 555 }, season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} }],
          normalized: [{ kind: 'cross_series', external_ids: { tvdb: 555, imdb: 'tt9999999' } }],
        },
      ],
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.05, s_alt: 0.85, proposed_action: { kind: 'replace' } },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const identifiedHeading = await screen.findByText('Identified as')
    const column = identifiedHeading.closest('div')!
    expect(within(column).getByText(/Different series/)).toBeInTheDocument()
    expect(within(column).getByRole('link', { name: 'TVDB' })).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/series/555')
  })

  it('"Identified as" shows the human override for an is_other verdict', async () => {
    getJobMock.mockResolvedValue(jobDetailHumanIdentFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const identifiedHeading = await screen.findByText('Identified as')
    const column = identifiedHeading.closest('div')!
    expect(within(column).getByText('S02E03E04')).toBeInTheDocument()
    expect(within(column).getByText('human override')).toBeInTheDocument()
  })

  // -- section 2: three-way text comparison, independently scrollable -----

  it('shows the transcript in the center text panel', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/Hello world/)).toBeInTheDocument()
    expect(screen.getByText(/Second line/)).toBeInTheDocument()
  })

  it('shows placeholder text when embedded subs / reference subs are unavailable', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('No embedded subtitles extracted.')).toBeInTheDocument()
    expect(screen.getByText('No reference subtitles compared.')).toBeInTheDocument()
  })

  it('renders embedded-subtitle cue text from a "subs" asset payload', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      assets: [
        ...jobDetailFixture.assets,
        { id: 9, type: 'subs', path: '/assets/embedded.srt', has_path: true, tool_meta: { language: 'en' }, payload: { cues: ['line one', 'line two'], language: 'en' } },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/line one/)).toBeInTheDocument()
  })

  it('renders reference-subtitle tracks with a source selector when more than one exists', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      reference_subtitles: [
        { label: 'S01E01', language: 'en', cues: ['ref cue one'] },
        { label: 'S01E02', language: 'en', cues: ['ref cue two'] },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/ref cue one/)).toBeInTheDocument()
    const select = screen.getByRole('combobox', { name: 'Reference subtitles source' })
    await userEvent.selectOptions(select, 'S01E02 (en)')
    expect(screen.getByText(/ref cue two/)).toBeInTheDocument()
  })

  // -- section 3: plugin results table --------------------------------------

  it('renders the plugin-results table (name/version, raw status, raw reason, zero-padded percent candidate confidence)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    expect(within(section).getByText(/whisper-transcript/)).toBeInTheDocument()
    expect(within(section).getByText('ok')).toBeInTheDocument()
    expect(within(section).getByText(/ocr-subs/)).toBeInTheDocument()
    expect(within(section).getByText('abstain')).toBeInTheDocument()
    expect(within(section).getByText('no subtitle track')).toBeInTheDocument()
    expect(within(section).getByText(/conf 92% · tvdb S01E01/)).toBeInTheDocument()
  })

  it('zero-pads single-digit season/episode numbers in candidate rows (S05E3 -> S05E03)', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.5, ident: { series: 'claimed', season: 5, episodes: [3] }, numbering: 'tvdb', evidence: {} }],
          normalized: [],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/S05E03/)).toBeInTheDocument()
  })

  it('the plugin-results table omits the in-series annotation and carries an evidence tooltip', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    expect(within(section).queryByText(/matches this series/)).not.toBeInTheDocument()
    const candidateLine = within(section).getByText(/conf 92% · tvdb S01E01/)
    expect(candidateLine).toHaveAttribute('title', JSON.stringify({}))
  })

  it('cross-series/junk annotations render real TVDB/IMDB links (not a raw external_ids JSON dump) — "no match" for junk', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 0.6, ident: { series: { tvdb: 555 }, season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} },
            { confidence: 0.1, ident: null, numbering: null, evidence: {} },
          ],
          normalized: [
            { kind: 'cross_series', external_ids: { tvdb: 555, imdb: 'tt9999999' } },
            { kind: 'junk' },
          ],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    expect(within(section).getByText(/different series/)).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: 'TVDB' })).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/series/555')
    expect(within(section).queryByText(/"tvdb":555/)).not.toBeInTheDocument()
    expect(within(section).getByText('no match')).toBeInTheDocument()
  })

  it('LLM-plugin rows (evidence.reasoning present) get a reasoning hover tooltip; other plugins do not', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'subs-llm',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [
            { confidence: 0.6, ident: { series: 'claimed', season: 1, episodes: [1] }, numbering: 'tvdb', evidence: { reasoning: 'dialogue references events unique to episode 5' } },
          ],
          normalized: [{ kind: 'in_series', episode_ids: [100] }],
        },
        jobDetailFixture.plugin_results[1], // ocr-subs, abstain, no candidates/evidence
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin results')).closest('section')!
    const llmCell = within(section).getByText(/subs-llm/).closest('td')!
    expect(llmCell).toHaveAttribute('title', 'dialogue references events unique to episode 5')
    expect(within(llmCell).getByText('ⓘ')).toBeInTheDocument()
    const ocrCell = within(section).getByText(/ocr-subs/).closest('td')!
    expect(ocrCell).not.toHaveAttribute('title')
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

  // -- identification statement (aggregate confidence sentence) -----------

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

  // -- section 4: phash / fingerprint, moved above probe summary -----------

  it('the Fingerprint section shows frames sampled, algo/version, and corpus membership with confidence/source', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    expect(within(section).getByText(/16 frames sampled, algo phash v1/)).toBeInTheDocument()
    expect(within(section).getByText(/in corpus \(auto, 97% confidence\)/)).toBeInTheDocument()
  })

  it('the Fingerprint section omits the corpus clause when there is no corpus entry', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, phash_corpus: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    expect(within(section).queryByText(/in corpus/)).not.toBeInTheDocument()
  })

  it('the Fingerprint section links the dupe\'s other file (via the series Sonarr-page fallback) when dupe_info is present', async () => {
    getJobMock.mockResolvedValue(jobDetailDupeFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Fingerprint')).closest('section')!
    const link = within(section).getByRole('link', { name: 'Other.S01E01.mkv' })
    expect(link).toHaveAttribute('href', 'http://sonarr.local/series/test-show')
    expect(within(section).getByText(/similarity 93%/)).toBeInTheDocument()
  })

  it('omits the Fingerprint section entirely when there is neither a frame hash nor a dupe hit', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, frame_hash: null, phash_corpus: null })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.queryByText('Fingerprint')).not.toBeInTheDocument()
  })

  it('Fingerprint (phash) renders above Probe summary', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    // The dialog renders in a portal outside RTL's container, hence document.body.
    const headings = [...document.body.querySelectorAll('h3')].map((h) => h.textContent)
    expect(headings.indexOf('Fingerprint')).toBeGreaterThanOrEqual(0)
    expect(headings.indexOf('Fingerprint')).toBeLessThan(headings.indexOf('Probe summary'))
  })

  // -- probe summary / remediation log (unchanged content, still present) -

  it('renders the probe summary at the bottom', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/duration 1200.5s/)).toBeInTheDocument()
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

  // -- section 7: debug datapack download, 2-click ------------------------

  it('the datapack download is disabled until the "include file paths" checkbox is checked (2-click)', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText(/whisper-transcript/)
    expect(screen.getByRole('button', { name: 'Download debug datapack' })).toBeDisabled()
    expect(screen.queryByRole('link', { name: 'Download debug datapack' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: /include file paths/i }))

    const link = screen.getByRole('link', { name: 'Download debug datapack' })
    expect(link).toHaveAttribute('href', '/api/v1/jobs/42/datapack')
    expect(link).toHaveAttribute('download')
  })
})
