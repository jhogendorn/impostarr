#!/usr/bin/env bash
# End-to-end demo/harness: real Sonarr + a synthetic 5-file library across a
# 6-episode season + stub transcription/chat-completions services +
# Impostarr (dry_run on, both identifier plugins enabled), all via docker
# compose. Puts both plugins (whisper-subs, subs-llm) through the full
# outcome matrix: matched (honest, both plugins agree), inconclusive (no
# audio/subs at all), remediated (mislabel, dry-run remap to an empty
# slot), and quarantine (mislabel whose correct slot is already occupied by
# a competing honest file — auto-remap refuses and proposes instead, also
# producing dupe_info for the near-identical pair). See
# demo/generate_media.py's module docstring for the full scenario table.
#
# Usage: bash demo/e2e.sh [--down]
#   --down   tear the stack down at the end (CI mode). Default: leave it
#            running for interactive inspection at http://localhost:8484.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPOSTARR_URL="http://localhost:8484"
TEARDOWN=0
[ "${1:-}" = "--down" ] && TEARDOWN=1

log() { echo "[e2e] $*"; }
fail() { echo "[e2e] FAIL: $*" >&2; exit 1; }

compose() { (cd "$DEMO_DIR" && docker compose "$@"); }

# -- 1. wipe + prep -----------------------------------------------------------

log "tearing down any previous stack"
compose down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf "$DEMO_DIR/volumes"
mkdir -p "$DEMO_DIR/volumes"/{media/tv,sonarr-config,config,assets,models,manifest,staging/refsubs,trash}
touch "$DEMO_DIR/volumes/.gitkeep"

# -- 2. build images -----------------------------------------------------------

log "building images (stub-services, impostarr)"
compose build

# -- 3. bring up Sonarr, seed it ----------------------------------------------

log "starting Sonarr"
compose up -d sonarr

log "seeding (series lookup, synthetic media, Sonarr import, refsubs, config render)"
bash "$DEMO_DIR/seed.sh"

# -- 4. bring up the rest of the stack -----------------------------------------

log "starting stub-services + impostarr"
compose up -d stub-services impostarr

log "waiting for impostarr healthz"
healthy=0
for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$IMPOSTARR_URL/api/v1/healthz" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then healthy=1; break; fi
  sleep 1
done
[ "$healthy" = "1" ] || fail "impostarr healthz never returned 200"
log "impostarr healthy"

# -- 5. trigger backfill --------------------------------------------------------

log "triggering backfill"
curl -sSf -X POST -H "Content-Type: application/json" -d '{"batch_size": 10}' \
  "$IMPOSTARR_URL/api/v1/instances/main/backfill" >/dev/null

# -- 6. poll queues until all 4 jobs reach a terminal state --------------------

TERMINAL_STATUSES="matched quarantine inconclusive error remediated"
log "polling /api/v1/status until 5 jobs are terminal (timeout 10m)"
done_count=0
for i in $(seq 1 300); do
  status_json="$(curl -sSf "$IMPOSTARR_URL/api/v1/status")"
  done_count=0
  for s in $TERMINAL_STATUSES; do
    n="$(echo "$status_json" | jq --arg s "$s" '.queues[$s] // 0')"
    done_count=$((done_count + n))
  done
  [ "$done_count" -ge 5 ] && break
  if [ $((i % 15)) -eq 0 ]; then
    log "  still waiting ($done_count/5 terminal): $(echo "$status_json" | jq -c .queues)"
  fi
  sleep 2
done
[ "$done_count" -ge 5 ] || fail "only $done_count/5 jobs reached a terminal state within 10m"
log "all 5 jobs terminal: $(echo "$status_json" | jq -c .queues)"

# -- 7. assertions ---------------------------------------------------------------

matched_items="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/matched" | jq '.items')"
matched_count="$(echo "$matched_items" | jq 'length')"
remediated_items="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/remediated" | jq '.items')"
remediated_count="$(echo "$remediated_items" | jq 'length')"
quarantine_items="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/quarantine" | jq '.items')"
quarantine_count="$(echo "$quarantine_items" | jq 'length')"
inconclusive_count="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/inconclusive" | jq '.items | length')"
error_count="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/error" | jq '.items | length')"

job_detail() { curl -sSf "$IMPOSTARR_URL/api/v1/jobs/$1"; }
# $1 = queue items json, $2 = "S01E0N" substring to match against file.sonarr_path
job_id_for_episode() {
  echo "$1" | jq -r --arg ep "$2" '.[] | select(.file.sonarr_path | contains($ep)) | .job_id'
}

pass=1

if [ "$matched_count" -ne 2 ]; then
  log "ASSERTION FAILED: expected 2 matched jobs (S01E01, S01E05), got $matched_count"
  pass=0
fi
if [ "$remediated_count" -ne 1 ]; then
  log "ASSERTION FAILED: expected 1 remediated job (S01E03), got $remediated_count"
  pass=0
fi
if [ "$quarantine_count" -ne 1 ]; then
  log "ASSERTION FAILED: expected 1 quarantine job (S01E04), got $quarantine_count"
  pass=0
fi
if [ "$inconclusive_count" -ne 1 ]; then
  log "ASSERTION FAILED: expected 1 inconclusive job (S01E02), got $inconclusive_count"
  pass=0
fi
if [ "$error_count" -ne 0 ]; then
  log "ASSERTION FAILED: expected 0 error jobs, got $error_count"
  pass=0
fi

# -- S01E03: remediated, dry-run remap to the empty S01E06 slot --------------

if [ "$remediated_count" -eq 1 ]; then
  job_id="$(echo "$remediated_items" | jq -r '.[0].job_id')"
  detail="$(job_detail "$job_id")"
  log_entries="$(echo "$detail" | jq -c '.verdict.remediation_log')"
  log "remediated job $job_id (S01E03) remediation_log: $log_entries"

  if ! echo "$log_entries" | jq -e 'all(.[]; .detail | startswith("DRY-RUN"))' >/dev/null; then
    log "ASSERTION FAILED: not every remediation_log entry is DRY-RUN prefixed"
    pass=0
  fi
  if ! echo "$log_entries" | jq -e '.[] | select(.step == "manual_import") | .detail | contains("S01E06")' >/dev/null; then
    log "ASSERTION FAILED: manual_import step does not name S01E06"
    pass=0
  fi
fi

# -- S01E04: quarantine, auto-remap proposed S01E05 but refused (occupied) --

e04_dupe_info="null"
if [ "$quarantine_count" -eq 1 ]; then
  job_id="$(echo "$quarantine_items" | jq -r '.[0].job_id')"
  detail="$(job_detail "$job_id")"
  log "quarantine job $job_id (S01E04) verdict: $(echo "$detail" | jq -c '.verdict')"

  if ! echo "$detail" | jq -e \
    '.verdict.remediation_log[] | select(.step == "occupied_check") | .detail | contains("S01E05")' \
    >/dev/null; then
    log "ASSERTION FAILED: S01E04's occupied_check step does not name S01E05"
    pass=0
  fi
  if ! echo "$detail" | jq -e '.verdict.proposed_action.kind == "remap"' >/dev/null; then
    log "ASSERTION FAILED: S01E04 has no proposed remap action"
    pass=0
  fi
  e04_dupe_info="$(echo "$detail" | jq -c '.verdict.dupe_info')"
fi

# -- S01E04/S01E05: near-identical content, dupe_info on at least one -------

e05_job_id="$(job_id_for_episode "$matched_items" "S01E05")"
e05_dupe_info="null"
if [ -z "$e05_job_id" ]; then
  log "ASSERTION FAILED: no matched job found for S01E05"
  pass=0
else
  e05_dupe_info="$(job_detail "$e05_job_id" | jq -c '.verdict.dupe_info')"
fi
if [ "$e04_dupe_info" = "null" ] && [ "$e05_dupe_info" = "null" ]; then
  log "ASSERTION FAILED: neither S01E04 nor S01E05 has dupe_info set (expected phash near-match)"
  pass=0
else
  log "dupe_info: S01E04=$e04_dupe_info S01E05=$e05_dupe_info"
fi

# -- S01E01: matched, both identifier plugins produced results --------------

e01_job_id="$(job_id_for_episode "$matched_items" "S01E01")"
if [ -z "$e01_job_id" ]; then
  log "ASSERTION FAILED: no matched job found for S01E01"
  pass=0
else
  e01_detail="$(job_detail "$e01_job_id")"
  if ! echo "$e01_detail" | jq -e \
    'any(.plugin_results[]; .name == "whisper-subs" and .status == "ok")' >/dev/null; then
    log "ASSERTION FAILED: S01E01 job missing an ok whisper-subs plugin_result"
    pass=0
  fi
  if ! echo "$e01_detail" | jq -e \
    'any(.plugin_results[]; .name == "subs-llm" and .status == "ok")' >/dev/null; then
    log "ASSERTION FAILED: S01E01 job missing an ok subs-llm plugin_result (subs-llm did not run)"
    pass=0
  fi
fi

# -- 8. summary --------------------------------------------------------------

echo
if [ "$pass" -eq 1 ]; then
  echo "[e2e] PASS: 2 matched, 1 inconclusive, 1 quarantine (occupied remap + dupe_info), 1 remediated (dry-run remap -> S01E06)"
else
  echo "[e2e] FAIL: see assertion failures above"
fi
echo "[e2e] UI: $IMPOSTARR_URL"

if [ "$TEARDOWN" -eq 1 ]; then
  log "tearing down (--down)"
  compose down -v --remove-orphans
else
  log "stack left running; teardown with: (cd demo && docker compose down -v)"
fi

[ "$pass" -eq 1 ]
