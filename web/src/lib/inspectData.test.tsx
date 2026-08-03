import { describe, expect, it } from 'vitest'
import type { AlternateCandidate } from './inspectData'
import { defaultPreviewEpisodeId, leadingEpisodeId } from './inspectData'
import { jobDetailFixture, jobDetailHumanIdentFixture } from '../components/testFixtures'

const altCandidate: AlternateCandidate = { episodeIds: [200], season: 5, episodes: [2], confidence: 0.468 }

describe('leadingEpisodeId (item 16: claimed-vs-alternate display bug)', () => {
  it('leads with the CLAIMED episode when s_claimed >= s_alt (job 14 case: 0.600 vs 0.468)', () => {
    const job = { ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.6, s_alt: 0.468 } }
    expect(leadingEpisodeId(job, [altCandidate])).toBe(job.file.episode_ids[0])
  })

  it('leads with the ALTERNATE candidate when s_alt > s_claimed', () => {
    const job = { ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.1, s_alt: 0.9 } }
    expect(leadingEpisodeId(job, [altCandidate])).toBe(200)
  })

  it('leads with the claimed episode when there is no alternate at all', () => {
    const job = { ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.3, s_alt: null } }
    expect(leadingEpisodeId(job, [])).toBe(job.file.episode_ids[0])
  })

  it('leads with the claimed episode on an exact tie (s_claimed == s_alt)', () => {
    const job = { ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.5, s_alt: 0.5 } }
    expect(leadingEpisodeId(job, [altCandidate])).toBe(job.file.episode_ids[0])
  })

  it('an explicit human is_other override always wins regardless of s_claimed/s_alt', () => {
    // human_ident targets season 2 episode 3; series_episodes must resolve it to an id.
    const job = {
      ...jobDetailHumanIdentFixture,
      series_episodes: [
        ...jobDetailHumanIdentFixture.series_episodes,
        { id: 700, season: 2, episode: 3, title: 'Human Pick', tvdb_id: null },
      ],
    }
    expect(leadingEpisodeId(job, [altCandidate])).toBe(700)
  })
})

describe('defaultPreviewEpisodeId (item 13 layered on item 16)', () => {
  it('defaults to the claimed episode (no refsubs anywhere) when claimed leads', () => {
    const job = { ...jobDetailFixture, verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.6, s_alt: 0.468 }, reference_subtitles: [] }
    expect(defaultPreviewEpisodeId(job, [altCandidate])).toBe(job.file.episode_ids[0])
  })

  it('defaults to the alternate when it leads AND has cached refsubs', () => {
    const job = {
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.1, s_alt: 0.9 },
      reference_subtitles: [{ label: 'S05E02', language: 'en', cues: [{ start_s: 0, text: 'x' }], episode_ids: [200] }],
    }
    expect(defaultPreviewEpisodeId(job, [altCandidate])).toBe(200)
  })

  it('regression (live job 14): claimed leads (0.6 vs 0.468) but has NO cached refsubs while its alternate does — must still show the CLAIMED episode, not silently swap to the alternate', () => {
    const job = {
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.6, s_alt: 0.468 },
      // claimed episode (100) has no cached track; the alternate (200) does.
      reference_subtitles: [{ label: 'S01E02', language: 'en', cues: [{ start_s: 0, text: 'x' }], episode_ids: [200] }],
    }
    const altWithRefsubs: AlternateCandidate = { episodeIds: [200], season: 1, episodes: [2], confidence: 0.468 }
    expect(defaultPreviewEpisodeId(job, [altWithRefsubs])).toBe(job.file.episode_ids[0])
  })

  it('falls back to a populated refsub track when the leading id (alt) has none cached', () => {
    const job = {
      ...jobDetailFixture,
      verdict: { ...jobDetailFixture.verdict!, s_claimed: 0.1, s_alt: 0.9 },
      reference_subtitles: [{ label: 'S01E01', language: 'en', cues: [{ start_s: 0, text: 'x' }], episode_ids: [100] }],
    }
    // 200 (the leading alt) has no cached refsubs, but 100 (the claimed
    // episode) does — never silently default to an empty view.
    expect(defaultPreviewEpisodeId(job, [altCandidate])).toBe(100)
  })
})
