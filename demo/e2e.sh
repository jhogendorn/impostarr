#!/usr/bin/env bash
# End-to-end demo/harness: real Sonarr + a synthetic 4-episode library (one
# deliberately mislabeled) + a stub transcriber + Impostarr (dry_run on),
# all via docker compose. Verifies Impostarr correctly matches the 3
# honestly-labelled episodes and catches + proposes-then-dry-run-remediates
# the mislabeled one.
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

log "building images (stub-transcriber, impostarr)"
compose build

# -- 3. bring up Sonarr, seed it ----------------------------------------------

log "starting Sonarr"
compose up -d sonarr

log "seeding (series lookup, synthetic media, Sonarr import, refsubs, config render)"
bash "$DEMO_DIR/seed.sh"

# -- 4. bring up the rest of the stack -----------------------------------------

log "starting stub-transcriber + impostarr"
compose up -d stub-transcriber impostarr

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
log "polling /api/v1/status until 4 jobs are terminal (timeout 10m)"
done_count=0
for i in $(seq 1 300); do
  status_json="$(curl -sSf "$IMPOSTARR_URL/api/v1/status")"
  done_count=0
  for s in $TERMINAL_STATUSES; do
    n="$(echo "$status_json" | jq --arg s "$s" '.queues[$s] // 0')"
    done_count=$((done_count + n))
  done
  [ "$done_count" -ge 4 ] && break
  if [ $((i % 15)) -eq 0 ]; then
    log "  still waiting ($done_count/4 terminal): $(echo "$status_json" | jq -c .queues)"
  fi
  sleep 2
done
[ "$done_count" -ge 4 ] || fail "only $done_count/4 jobs reached a terminal state within 10m"
log "all 4 jobs terminal: $(echo "$status_json" | jq -c .queues)"

# -- 7. assertions ---------------------------------------------------------------

matched_items="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/matched" | jq '.items')"
matched_count="$(echo "$matched_items" | jq 'length')"
remediated_items="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/remediated" | jq '.items')"
remediated_count="$(echo "$remediated_items" | jq 'length')"

quarantine_count="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/quarantine" | jq '.items | length')"
inconclusive_count="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/inconclusive" | jq '.items | length')"
error_count="$(curl -sSf "$IMPOSTARR_URL/api/v1/queues/error" | jq '.items | length')"

pass=1

if [ "$matched_count" -ne 3 ]; then
  log "ASSERTION FAILED: expected 3 matched jobs, got $matched_count"
  pass=0
fi
if [ "$remediated_count" -ne 1 ]; then
  log "ASSERTION FAILED: expected 1 remediated job, got $remediated_count"
  pass=0
fi
if [ "$quarantine_count" -ne 0 ] || [ "$inconclusive_count" -ne 0 ] || [ "$error_count" -ne 0 ]; then
  log "ASSERTION FAILED: expected 0 quarantine/inconclusive/error, got $quarantine_count/$inconclusive_count/$error_count"
  pass=0
fi

if [ "$remediated_count" -eq 1 ]; then
  job_id="$(echo "$remediated_items" | jq -r '.[0].job_id')"
  detail="$(curl -sSf "$IMPOSTARR_URL/api/v1/jobs/$job_id")"
  log_entries="$(echo "$detail" | jq -c '.verdict.remediation_log')"
  log "remediated job $job_id remediation_log: $log_entries"

  if ! echo "$log_entries" | jq -e 'all(.[]; .detail | startswith("DRY-RUN"))' >/dev/null; then
    log "ASSERTION FAILED: not every remediation_log entry is DRY-RUN prefixed"
    pass=0
  fi
  if ! echo "$log_entries" | jq -e '.[] | select(.step == "manual_import") | .detail | contains("S01E05")' >/dev/null; then
    log "ASSERTION FAILED: manual_import step does not name S01E05"
    pass=0
  fi
fi

# -- 8. summary --------------------------------------------------------------

echo
if [ "$pass" -eq 1 ]; then
  echo "[e2e] PASS: 3 matched, 1 remediated (dry-run remap -> S01E05)"
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
