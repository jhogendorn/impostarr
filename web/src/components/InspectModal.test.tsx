import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import InspectModal from './InspectModal'
import { jobDetailDupeFixture, jobDetailFixture, jobDetailHumanIdentFixture, jobDetailMatchedFixture } from './testFixtures'

const { getJobMock } = vi.hoisted(() => ({ getJobMock: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, getJob: getJobMock }
})

describe('InspectModal (v4)', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  // -- structural order: sticky header (A+B), then C, D, E, F -------------

  it('the sticky header holds the action bar, with no old title/subtitle identity row above it', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const applyRemap = await screen.findByRole('combobox', { name: 'Apply Remap' })
    const stickyHeader = applyRemap.closest('.sticky')!
    expect(stickyHeader).toHaveClass('top-0')
    // The old filename/title heading is gone entirely — only a visually
    // hidden DialogTitle remains, for assistive tech.
    expect(screen.queryByText('Show.S01E01.mkv')).not.toBeInTheDocument()
  })

  it('renders a Close button that calls onClose', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue(jobDetailFixture)
    const onClose = vi.fn()
    render(<InspectModal jobId={42} open onClose={onClose} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    await user.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).toHaveBeenCalled()
  })

  it('the dialog panel carries the elevated indigo glow treatment', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    const panel = document.body.querySelector('.max-w-7xl')
    expect(panel).toHaveClass('glow-elevated')
  })

  it('section order: proposed-action banner, then plugin results, then details, then datapack (comparison section always present between action bar and plugin results)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture) // has a remap proposal
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    const headings = [...document.body.querySelectorAll('h3')].map((h) => h.textContent)
    expect(headings.indexOf('Plugin Results')).toBeGreaterThanOrEqual(0)
    expect(headings.indexOf('Details')).toBeGreaterThan(headings.indexOf('Plugin Results'))
  })

  it('renders the Proposed Action banner even with no proposal at all — "No Proposed Action" + "Manual Review" (item 5)', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: null } })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('No Proposed Action')).toBeInTheDocument()
    expect(screen.getByText('Manual Review')).toBeInTheDocument()
    expect(screen.queryByText(/Remap to/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Replace —/)).not.toBeInTheDocument()
  })

  it('shows the proposed-action banner with "Manual Review" (no apply_at) tinted for a remap proposal, S01E01 linked to its tvdb id', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture) // proposed_action: remap -> episode 100 (tvdb_id 378653)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const description = await screen.findByText((_, el) => el?.tagName === 'SPAN' && el.textContent === 'Remap to S01E01')
    expect(description).toBeInTheDocument()
    expect(within(description).getByRole('link', { name: 'E01' })).toHaveAttribute(
      'href',
      'https://thetvdb.com/dereferrer/episode/378653',
    )
    expect(screen.getByText('Manual Review')).toBeInTheDocument()
  })

  it('shows "Auto Apply In: <countdown>" instead of "Manual Review" once apply_at is present', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, apply_at: new Date(Date.now() + 3600_000).toISOString() },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText(/Auto Apply In:/)).toBeInTheDocument()
    expect(screen.queryByText('Manual Review')).not.toBeInTheDocument()
  })

  it('tints the banner red for a replace proposal, indigo for a remap proposal', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, proposed_action: { kind: 'replace' } } })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const banner = (await screen.findByText(/Replace —/)).closest('div')!.parentElement!
    expect(banner).toHaveClass('bg-red-500/10')
  })

  // -- rename sweep: Reidentify, never "Rerun" -----------------------------

  it('never renders the word "Rerun" anywhere in the panel (renamed to Reidentify)', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, job: { ...jobDetailFixture.job, status: 'matched' } })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('button', { name: 'Reidentify' })
    expect(document.body.textContent).not.toMatch(/\bRerun\b/)
  })

  // -- section C: comparison — LHS/RHS, scrubber, confidence badge --------

  it('LHS header reads "Sonarr {InstanceName} Label", Title-Cased with no quotes around the instance name', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture) // instance: 'main'
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Sonarr Main Label')).toBeInTheDocument()
    expect(screen.queryByText(/Sonarr 'Main' Label/)).not.toBeInTheDocument()
  })

  it('LHS shows the labelled episode ident, DB links, and a Framegrabs strip with a frame-count badge', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect((await screen.findAllByText('Pilot', { exact: false })).length).toBeGreaterThan(0)
    expect(screen.getByText('Framegrabs')).toBeInTheDocument()
    expect(screen.getByText('2 frames')).toBeInTheDocument()
    expect(screen.getAllByAltText(/frame \d/)).toHaveLength(2)
  })

  it('RHS header reads "Content Identity"; there is no selector in the RHS panel itself — it only previews Apply Remap\'s selection (item 1)', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const heading = await screen.findByText('Content Identity')
    expect(heading.closest('div')?.parentElement).not.toBeNull()
    // No native select, and no ARIA-labelled "Content identity episode"
    // combobox anymore — Apply Remap (in the Action Bar) is the only one.
    expect(document.querySelector('select')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Content identity episode' })).not.toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toBeInTheDocument()
  })

  it('RHS defaults to the CLAIMED episode (with a "Matches Sonarr label" note) when s_claimed >= s_alt — item 16 fix', async () => {
    // jobDetailFixture: s_claimed 0.92 >= s_alt 0.1 — the claimed episode
    // (S01E01 Pilot) leads, even though a plugin also suggested S01E05 as
    // an alternate candidate below.
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null },
      ],
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.1, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} }],
          normalized: [{ kind: 'in_series', episode_ids: [999] }],
        },
      ],
      episode_labels: { ...jobDetailFixture.episode_labels, '999': { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null } },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText('Content Identity')
    expect(screen.getAllByText('Pilot', { exact: false }).length).toBeGreaterThan(0)
    expect(screen.queryByText('Fifth', { exact: false })).not.toBeInTheDocument()
    expect(screen.getByText('Matches Sonarr label')).toBeInTheDocument()
  })

  it('RHS defaults to the leading ALTERNATE candidate when s_alt > s_claimed', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.1, s_alt: 0.9 },
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null },
      ],
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.9, ident: { series: 'claimed', season: 1, episodes: [5] }, numbering: 'tvdb', evidence: {} }],
          normalized: [{ kind: 'in_series', episode_ids: [999] }],
        },
      ],
      episode_labels: { ...jobDetailFixture.episode_labels, '999': { id: 999, season: 1, episode: 5, title: 'Fifth', tvdb_id: null } },
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByText('Content Identity')
    expect(screen.getAllByText('Fifth', { exact: false }).length).toBeGreaterThan(0)
    expect(screen.queryByText('Matches Sonarr label')).not.toBeInTheDocument()
  })

  it('item 13: when the ALTERNATE leads, never defaults to an empty refsub view when a populated one exists — auto-previews S01E02 even though the top alternate itself has none cached', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      // alt leads (0.9 > 0.1) — item 13's refsub-preference still applies
      // to this path (item 16 only exempts the CLAIMED-leads case).
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.1, s_alt: 0.9 },
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 200, season: 1, episode: 2, title: 'Second', tvdb_id: null },
      ],
      reference_subtitles: [{ label: 'S01E02', language: 'en', cues: [{ start_s: 1, text: 'second episode ref line' }], episode_ids: [200] }],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    expect(await screen.findByText(/second episode ref line/)).toBeInTheDocument()
    expect(screen.queryByText('No reference subtitles cached for this episode.')).not.toBeInTheDocument()
  })

  it('item 16 + 13 interaction (regression, live job 14): when the CLAIMED episode leads but has no cached refsubs while an alternate does, the RHS still shows the claimed episode plainly with "no reference subtitles cached" — never silently swapped to the alternate', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.6, s_alt: 0.468 },
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 200, season: 1, episode: 2, title: 'Second', tvdb_id: null },
      ],
      reference_subtitles: [{ label: 'S01E02', language: 'en', cues: [{ start_s: 1, text: 'second episode ref line' }], episode_ids: [200] }],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    expect(screen.getByText('Matches Sonarr label')).toBeInTheDocument()
    expect(screen.getByText('No reference subtitles cached for this episode.')).toBeInTheDocument()
    expect(screen.queryByText(/second episode ref line/)).not.toBeInTheDocument()
  })

  it('shows the empty-refsubs message when truly nothing is cached anywhere', async () => {
    getJobMock.mockResolvedValue({ ...jobDetailFixture, reference_subtitles: [] })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    expect(screen.getByText('No reference subtitles cached for this episode.')).toBeInTheDocument()
  })

  it('previewing a different episode via Apply Remap drives the RHS ident text — WITHOUT calling the API (two-step, item 1)', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      series_episodes: [
        { id: 100, season: 1, episode: 1, title: 'Pilot', tvdb_id: 378653 },
        { id: 200, season: 1, episode: 2, title: 'Second', tvdb_id: null },
        { id: 300, season: 1, episode: 3, title: 'Third', tvdb_id: null },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const combobox = await screen.findByRole('combobox', { name: 'Apply Remap' })
    await user.click(combobox)
    await user.type(combobox, 'Third')
    await user.click(screen.getByRole('option', { name: 'S01E03 - Third' }))

    expect(screen.getAllByText('Third', { exact: false }).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Apply Remap to S01E03' })).toBeInTheDocument()
  })

  it('per-line hover tooltips show the timestamp on all three text panels; the transcript has no inline [0.0] numbers', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const line = await screen.findByText('Hello world')
    expect(line).toHaveAttribute('title', '0:00')
    expect(document.body.textContent).not.toMatch(/\[0\.0\]/)
  })

  it('shows a range-coloured confidence figure', async () => {
    getJobMock.mockResolvedValue(jobDetailMatchedFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText('Confidence')).toBeInTheDocument()
    expect(screen.getByText('97%')).toHaveClass('text-emerald-400')
  })

  // -- section D: plugin results — no Reason column, tooltip instead ------

  it('the Plugin Results table has no Reason column; the abstain reason is a tooltip on the Status cell', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin Results')).closest('section')!
    expect(within(section).queryByText('Reason')).not.toBeInTheDocument()
    const statusCell = within(section).getByText('abstain')
    expect(statusCell).toHaveAttribute('title', 'no subtitle track')
  })

  it('cross-series references render as real TVDB/IMDB links', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      plugin_results: [
        {
          name: 'whisper-transcript',
          version: '1.0.0',
          status: 'ok',
          reason: null,
          candidates: [{ confidence: 0.6, ident: { series: { tvdb: 555 }, season: 1, episodes: [1] }, numbering: 'tvdb', evidence: {} }],
          normalized: [{ kind: 'cross_series', external_ids: { tvdb: 555, imdb: 'tt9999999' } }],
        },
      ],
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Plugin Results')).closest('section')!
    const link = within(section).getByRole('link', { name: 'TVDB' })
    expect(link).toHaveAttribute('href', 'https://thetvdb.com/dereferrer/series/555')
  })

  // -- section E: details (fingerprint + probe + remediation log) ---------

  it('the Details section merges fingerprint, probe summary, and remediation log under one heading', async () => {
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Details')).closest('section')!
    expect(within(section).getByText(/16 frames sampled, algo phash v1/)).toBeInTheDocument()
    expect(within(section).getByText(/duration 1200.5s/)).toBeInTheDocument()
    expect(within(section).getByText(/interruption_guard/)).toBeInTheDocument()
  })

  it('the Details section links the dupe\'s other file when dupe_info is present', async () => {
    getJobMock.mockResolvedValue(jobDetailDupeFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    const section = (await screen.findByText('Details')).closest('section')!
    expect(within(section).getByRole('link', { name: 'Other.S01E01.mkv' })).toHaveAttribute('href', 'http://sonarr.local/series/test-show')
  })

  it('omits the Details section entirely when there is nothing to show', async () => {
    getJobMock.mockResolvedValue({
      ...jobDetailFixture,
      frame_hash: null,
      phash_corpus: null,
      verdict: { ...jobDetailFixture.verdict!, remediation_log: [] },
      assets: jobDetailFixture.assets.filter((a) => a.type !== 'probe'),
    })
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    expect(screen.queryByText('Details')).not.toBeInTheDocument()
  })

  // -- section F: debug datapack — bare checkbox, no label text -----------

  it('the datapack checkbox has no visible label text (only an aria-label) and arms the download', async () => {
    const user = userEvent.setup()
    getJobMock.mockResolvedValue(jobDetailFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    await screen.findByRole('combobox', { name: 'Apply Remap' })
    expect(screen.queryByText(/enable download/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download Debug Datapack' })).toBeDisabled()

    await user.click(screen.getByRole('checkbox', { name: 'Enable debug datapack download' }))

    const link = screen.getByRole('link', { name: 'Download Debug Datapack' })
    expect(link).toHaveAttribute('href', '/api/v1/jobs/42/datapack')
    expect(link).toHaveAttribute('download')
  })

  // -- human is_other override still flows through ------------------------

  it('shows the Apply Remap dropdown for a human is_other verdict (human_ident, no proposed_action)', async () => {
    getJobMock.mockResolvedValue(jobDetailHumanIdentFixture)
    render(<InspectModal jobId={42} open onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText((_, el) => el?.tagName === 'SPAN' && el.textContent === 'Remap to S02E03E04')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Apply Remap' })).toBeInTheDocument()
  })
})
