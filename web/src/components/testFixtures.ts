/** Shared fixtures for component tests. Not itself a test file. */
import type { JobDetail, LogRecord, QueuePage, StatusResponse } from '../api/types'

export const statusFixture: StatusResponse = {
  instances: [
    { name: 'main', url: 'http://sonarr.local', history_watermark: '2026-08-01T00:00:00Z', backfill_cursor: null },
  ],
  queues: {
    hold: 1,
    pending: 2,
    active: 3,
    matched: 4,
    quarantine: 5,
    inconclusive: 6,
    error: 7,
    remediated: 8,
  },
  workers: { pool_size: 2 },
  dry_run: false,
}

export const dryRunStatusFixture: StatusResponse = { ...statusFixture, dry_run: true }

export const logRecordsFixture: LogRecord[] = [
  { ts: '2026-08-02T00:00:00Z', level: 'INFO', logger: 'impostarr.worker', message: 'claimed job 42' },
  {
    ts: '2026-08-02T00:00:01Z',
    level: 'WARNING',
    logger: 'impostarr.sonarr.client',
    message: 'DRY-RUN: would DELETE /episodefile/9001',
  },
  { ts: '2026-08-02T00:00:02Z', level: 'ERROR', logger: 'impostarr.pipeline', message: 'plugin crashed' },
]

export const queuePageFixture: QueuePage = {
  total: 4,
  items: [
    {
      job_id: 1,
      status: 'quarantine',
      file: { series_id: 10, sonarr_path: '/media/Show/Season 01/Show.S01E01.mkv', episode_ids: [100] },
      verdict: { s_claimed: 0.92, s_alt: 0.05, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 2,
      status: 'quarantine',
      file: { series_id: 11, sonarr_path: '/media/Show2/Season 02/Show2.S02E02.mkv', episode_ids: [200, 201] },
      verdict: { s_claimed: 0.5, s_alt: 0.3, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 3,
      status: 'quarantine',
      file: { series_id: 12, sonarr_path: '/media/Show3/Season 03/Show3.S03E03.mkv', episode_ids: [300] },
      verdict: { s_claimed: 0.1, s_alt: 0.05, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 4,
      status: 'quarantine',
      file: { series_id: 13, sonarr_path: '/media/Show4/Season 04/Show4.S04E04.mkv', episode_ids: [400] },
      verdict: null,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
  ],
}

export const jobDetailFixture: JobDetail = {
  job: { id: 42, status: 'quarantine', attempts: 1, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-02T00:00:00Z' },
  file: {
    series_id: 10,
    episode_ids: [100],
    episode_file_id: 555,
    sonarr_path: '/media/Show/Season 01/Show.S01E01.mkv',
    local_path: '/local/Show.S01E01.mkv',
    size: 123456,
    content_hash: 'abc123',
    quality: 'WEBDL-1080p',
    languages: ['en'],
    history_id: 1,
    download_id: 'dl1',
    source_title: 'Show.S01E01.WEBDL',
    indexer: 'idx',
    guid: 'guid1',
  },
  plugin_results: [
    {
      name: 'whisper-transcript',
      version: '1.0.0',
      status: 'ok',
      reason: null,
      candidates: [
        {
          confidence: 0.92,
          ident: { series: 'claimed', season: 1, episodes: [1] },
          numbering: 'tvdb',
          evidence: {},
        },
      ],
      normalized: [{ kind: 'in_series', episode_ids: [100] }],
    },
    {
      name: 'ocr-subs',
      version: '1.0.0',
      status: 'abstain',
      reason: 'no subtitle track',
      candidates: [],
      normalized: [],
    },
  ],
  verdict: {
    s_claimed: 0.92,
    s_alt: 0.1,
    outcome: 'quarantine',
    proposed_action: { kind: 'remap', target_episode_ids: [100] },
    remediation_log: [
      { step: 'interruption_guard', ok: true, detail: 'no interruption in progress', ts: '2026-08-02T00:00:00Z' },
    ],
    source: 'auto',
    human_ident: null,
  },
  assets: [
    {
      id: 1,
      type: 'transcript',
      path: null,
      has_path: false,
      tool_meta: null,
      payload: {
        segments: [
          { start: 0, end: 2, text: 'Hello world' },
          { start: 2, end: 4, text: 'Second line' },
        ],
        language: 'en',
      },
    },
    { id: 2, type: 'frames', path: '/assets/frame1.jpg', has_path: true, tool_meta: null, payload: null },
    { id: 3, type: 'frames', path: '/assets/frame2.jpg', has_path: true, tool_meta: null, payload: null },
    {
      id: 4,
      type: 'probe',
      path: null,
      has_path: false,
      tool_meta: null,
      payload: {
        format: { duration: '1200.5', format_name: 'matroska,webm' },
        streams: [{ codec_type: 'video' }, { codec_type: 'audio' }],
      },
    },
  ],
  frame_hash_present: true,
}

/** quarantine job carrying a human `is_other` verdict — human_ident set,
 * no proposed_action (backend only resolves target_episode_ids at
 * approve-time), source: 'human'. */
export const jobDetailHumanIdentFixture: JobDetail = {
  ...jobDetailFixture,
  verdict: {
    s_claimed: null,
    s_alt: null,
    outcome: 'quarantine',
    proposed_action: null,
    remediation_log: null,
    source: 'human',
    human_ident: { season: 2, episodes: [3, 4] },
  },
}
