/** Shared fixtures for component tests. Not itself a test file. */
import type { ActiveJob, JobDetail, LogRecord, QueuePage, StatusResponse, TrashItem, TrashPage } from '../api/types'

export const statusFixture: StatusResponse = {
  instances: [
    {
      name: 'main',
      url: 'http://sonarr.local',
      history_watermark: '2026-08-01T00:00:00Z',
      backfill_cursor: null,
      last_polled_at: '2026-08-02T23:55:00Z',
      last_backfilled_at: '2026-08-01T12:00:00Z',
    },
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
  summary: { unprocessed: 6, processed: 30 },
  system: { cpu_percent: 42.5, mem_percent: 61.2 },
  approval_required: false,
  active_jobs: [],
  workers: { pool_size: 2 },
  dry_run: false,
  trash_count: 2,
}

export const dryRunStatusFixture: StatusResponse = { ...statusFixture, dry_run: true }

export const activeJobsFixture: ActiveJob[] = [
  {
    job_id: 101,
    instance: 'main',
    series_id: 10,
    sonarr_path: '/media/Show/Season 01/Show.S01E01.mkv',
    claimed_by: 'worker-1',
    claimed_at: '2026-08-02T23:59:00Z',
    elapsed_s: 45,
  },
  {
    job_id: 102,
    instance: 'main',
    series_id: 11,
    sonarr_path: '/media/Show2/Season 02/Show2.S02E02.mkv',
    claimed_by: 'worker-2',
    claimed_at: '2026-08-02T23:58:00Z',
    elapsed_s: 105,
  },
]

export const statusWithActiveJobsFixture: StatusResponse = {
  ...statusFixture,
  active_jobs: activeJobsFixture,
}

export const logRecordsFixture: LogRecord[] = [
  { ts: '2026-08-02T00:00:00Z', level: 'INFO', logger: 'impostarr.worker', message: 'claimed job 42', exc: null },
  {
    ts: '2026-08-02T00:00:01Z',
    level: 'WARNING',
    logger: 'impostarr.sonarr.client',
    message: 'DRY-RUN: would DELETE /episodefile/9001',
    exc: null,
  },
  {
    ts: '2026-08-02T00:00:02Z',
    level: 'ERROR',
    logger: 'impostarr.pipeline',
    message: 'plugin crashed',
    exc: 'Traceback (most recent call last):\n  File "pipeline.py", line 42, in run\n    plugin.identify(job)\nValueError: boom',
  },
]

export const queuePageFixture: QueuePage = {
  total: 4,
  page_size: 50,
  items: [
    {
      job_id: 1,
      status: 'quarantine',
      instance: 'main',
      file: { series_id: 10, sonarr_path: '/media/Show/Season 01/Show.S01E01.mkv', episode_ids: [100] },
      verdict: { s_claimed: 0.92, s_alt: 0.05, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 2,
      status: 'quarantine',
      instance: 'main',
      file: { series_id: 11, sonarr_path: '/media/Show2/Season 02/Show2.S02E02.mkv', episode_ids: [200, 201] },
      verdict: { s_claimed: 0.5, s_alt: 0.3, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 3,
      status: 'quarantine',
      instance: 'backup',
      file: { series_id: 12, sonarr_path: '/media/Show3/Season 03/Show3.S03E03.mkv', episode_ids: [300] },
      verdict: { s_claimed: 0.1, s_alt: 0.05, outcome: 'quarantine' },
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
    {
      job_id: 4,
      status: 'quarantine',
      instance: 'main',
      file: { series_id: 13, sonarr_path: '/media/Show4/Season 04/Show4.S04E04.mkv', episode_ids: [400] },
      verdict: null,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-02T00:00:00Z',
    },
  ],
}

export const jobDetailFixture: JobDetail = {
  job: { id: 42, status: 'quarantine', attempts: 1, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-02T00:00:00Z' },
  instance: 'main',
  external_ids: { title: 'Test Show', tvdb_id: 81189, imdb_id: 'tt0903747', tmdb_id: 1396 },
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
    dupe_info: null,
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
    {
      id: 2,
      type: 'frames',
      path: '/assets/frame1.jpg',
      has_path: true,
      tool_meta: { timestamp_s: 65 },
      payload: null,
    },
    {
      id: 3,
      type: 'frames',
      path: '/assets/frame2.jpg',
      has_path: true,
      tool_meta: { timestamp_s: 130 },
      payload: null,
    },
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
  frame_hash: { algo: 'phash', version: 1, n_frames: 16 },
  phash_corpus: { confidence: 0.97, source: 'auto' },
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
    dupe_info: null,
  },
}

/** matched job with no remediation/proposal, for plain-language outcome mapping tests. */
export const jobDetailMatchedFixture: JobDetail = {
  ...jobDetailFixture,
  job: { ...jobDetailFixture.job, status: 'matched' },
  verdict: {
    s_claimed: 0.97,
    s_alt: 0.02,
    outcome: 'matched',
    proposed_action: null,
    remediation_log: null,
    source: 'auto',
    human_ident: null,
    dupe_info: null,
  },
}

/** job with a dupe_info hit on its verdict. */
export const jobDetailDupeFixture: JobDetail = {
  ...jobDetailFixture,
  verdict: {
    ...jobDetailFixture.verdict!,
    dupe_info: { duplicate_of_file_id: 999, similarity: 0.93, sonarr_path: '/media/Other/Season 01/Other.S01E01.mkv' },
  },
}

export const trashItemsFixture: TrashItem[] = [
  {
    id: 1,
    instance: 'main',
    original_path: '/media/Show/Season 01/Show.S01E01.mkv',
    trash_path: '/trash/main/Show.S01E01.mkv-1',
    series_id: 10,
    episode_ids: [100],
    size: 123456,
    trashed_at: '2026-08-01T00:00:00Z',
    expires_at: '2026-08-15T00:00:00Z',
    expires_in_s: 1209600,
  },
  {
    id: 2,
    instance: 'main',
    original_path: '/media/Show2/Season 02/Show2.S02E02.mkv',
    trash_path: '/trash/main/Show2.S02E02.mkv-2',
    series_id: 11,
    episode_ids: [200, 201],
    size: 654321,
    trashed_at: '2026-07-01T00:00:00Z',
    expires_at: '2026-08-02T23:00:00Z',
    expires_in_s: -3600,
  },
]

export const trashPageFixture: TrashPage = {
  total: 2,
  page_size: 50,
  items: trashItemsFixture,
}
