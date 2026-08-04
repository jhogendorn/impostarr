# Integration facts

Hard-won behaviour of the external services Impostarr talks to. Every item
here was discovered against a live system after passing mocked tests — treat
this file as the record so it isn't rediscovered painfully.

## Sonarr

- **History `eventType` must be numeric.** `eventType=downloadFolderImported`
  returns **400**; `eventType=3` works. The string form is what the API docs
  imply and what mocks happily accept.
- **`sortKey=id` is not supported.** Sonarr silently substitutes `date` and
  echoes `sortKey: "date"` in the response, so any pagination logic assuming
  id ordering is quietly wrong. We sort by `date` descending and page until a
  full page yields no records above the watermark (date order and id order are
  not guaranteed identical, so a mid-page early exit could skip records).
- **Query params must be alphabetical.** Non-canonical ordering can return a
  301 to an HTML page; a client without `follow_redirects` then fails on JSON
  parsing with an opaque error.
- **Episodes carry `tvdbId` per episode.** This is how per-episode deep links
  are built (`thetvdb.com/dereferrer/episode/<id>`). Our own `Episode` model
  originally omitted the field, which led to the repeated but wrong conclusion
  that per-episode links were impossible.
- **Blocklisting requires grab history.** `POST /history/failed/{id}` is
  history-record based, so files imported by a library rescan (rather than a
  grab) cannot be blocklisted. Discovery captures grab metadata when it
  exists; `replace` falls back to delete + re-search and records the job as
  unblocklistable.
- **There is no atomic "reassign this episode file" operation.** Remap is a
  choreography: hardlink into staging → delete the original episodeFile
  record → `ManualImport` the staged file with `importMode: move`. Deleting an
  episodeFile deletes the file on disk, so the hardlink must exist first.
- **Sonarr races its own post-add scan.** Firing an explicit `RescanSeries`
  right after adding a series can import the same file twice, leaving an
  orphaned episodefile row. Wait for the episode list's `hasFile`/
  `episodeFileId` mapping to be stable across two polls instead.
- **Video-only files are permanently unimportable** — `HasAudioTrack`
  rejects them, with nothing useful in the Info-level log.
- Library rescan imports do **not** produce `downloadFolderImported` history
  events, so the poll path never sees them; backfill is the correct discovery
  mechanism for pre-existing libraries.

## OpenSubtitles

- **A `User-Agent` is mandatory.** Requests without one get a 403
  `kong-user-agent-block` from their gateway.
- **Login is rate limited to ~1 req/s per IP.** Concurrent plugin calls will
  stampede it; we single-flight token acquisition behind a lock and back off
  once on 429.
- **All API calls need global pacing.** Even with login single-flighted,
  concurrent search/download calls draw 429s. We serialise API requests behind
  a lock with a minimum inter-request interval (default 1.1s) and retry once
  on 429.
- **`parent_tvdb_id` search is broken server-side** (returns 400 "Not enough
  parameters" for valid requests). Search strategy is imdb-first
  (`parent_imdb_id`, numeric, no `tt`), then tvdb, then a title `query`, with
  the strategy that served the result logged.
- **The free tier allows 20 downloads/day** — roughly one 20-episode season.
  This is the practical pacing constraint on any wide scan; the daily-quota
  guard persists across restarts.
- An account's username/password is **not** an API key; the REST API also
  needs an app-level consumer key registered separately.

## LLM providers

- No provider exposes remaining credit/balance via API. Spend must be metered
  locally from token usage. Usage-reporting APIs exist (OpenAI organization
  Usage, Anthropic Usage & Cost) but require admin-scoped keys distinct from
  inference keys.
- Anthropic's OpenAI-compatible endpoint means provider failover is a
  `base_url`/model change, not a new client.
- See `docs/llm-identification.md` for what these models can and cannot be
  asked to do reliably.

## Container / runtime

- `faster-whisper` (CTranslate2) supports CPU and CUDA only — there is no
  Intel/AMD GPU backend. Non-NVIDIA hosts run CPU, which is why whisper is
  always installed in the image: transcription must degrade to slow, never to
  a dead identifier.
- On CPU, force `compute_type="int8"`; the default float16 falls back to
  float32 and runs several times slower.
- Concurrent workers each lazily loading their own whisper model will OOM a
  4Gi container. The model load is locked and inference is bounded by a
  semaphore.
- `pywhispercpp` ships cp312 manylinux wheels — no source build needed.
