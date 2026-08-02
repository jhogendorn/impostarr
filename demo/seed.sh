#!/usr/bin/env bash
# Seeds the demo: waits for Sonarr, decides the series (with a SkyHook-down
# fallback), generates the synthetic library for that series, adds it to
# Sonarr, waits for the 4 on-disk files to be imported, places reference
# subtitles, and renders volumes/config/impostarr.yml with Sonarr's API key.
#
# Expects Sonarr already up (docker compose up -d sonarr) on SONARR_URL.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SONARR_URL="${SONARR_URL:-http://localhost:8989}"
PRIMARY_TERM="${SERIES_TERM:-Pioneer One}"
FALLBACK_TERM="Firefly"
CONFIG_XML="$DEMO_DIR/volumes/sonarr-config/config.xml"

log() { echo "[seed] $*" >&2; }

# -- wait for Sonarr's auto-generated API key --------------------------------

log "waiting for Sonarr API key ($CONFIG_XML)"
for _ in $(seq 1 90); do
  if [ -f "$CONFIG_XML" ] && grep -q '<ApiKey>' "$CONFIG_XML"; then
    break
  fi
  sleep 1
done
if [ ! -f "$CONFIG_XML" ] || ! grep -q '<ApiKey>' "$CONFIG_XML"; then
  log "FAIL: Sonarr never wrote an API key to $CONFIG_XML"
  exit 1
fi
API_KEY="$(grep -oE '<ApiKey>[^<]+</ApiKey>' "$CONFIG_XML" | sed -E 's#</?ApiKey>##g')"
log "got API key"

curl_sonarr() {
  curl -sS -H "X-Api-Key: $API_KEY" "$@"
}

log "waiting for Sonarr API to respond"
for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H "X-Api-Key: $API_KEY" "$SONARR_URL/api/v3/system/status" || true)"
  [ "$code" = "200" ] && break
  sleep 1
done
if [ "$code" != "200" ]; then
  log "FAIL: Sonarr API never came up (last status: $code)"
  exit 1
fi

# -- series lookup, with a SkyHook-down fallback -----------------------------

lookup() {
  local term="$1"
  local encoded
  encoded="$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$term")"
  curl_sonarr "$SONARR_URL/api/v3/series/lookup?term=$encoded"
}

log "looking up series '$PRIMARY_TERM'"
LOOKUP_JSON="$(lookup "$PRIMARY_TERM")"
if [ "$(echo "$LOOKUP_JSON" | jq 'length')" = "0" ]; then
  log "WARNING: no SkyHook results for '$PRIMARY_TERM'; falling back to '$FALLBACK_TERM'"
  LOOKUP_JSON="$(lookup "$FALLBACK_TERM")"
  if [ "$(echo "$LOOKUP_JSON" | jq 'length')" = "0" ]; then
    log "FAIL: no SkyHook results for '$FALLBACK_TERM' either"
    exit 1
  fi
fi

RESULT="$(echo "$LOOKUP_JSON" | jq '.[0]')"
TITLE="$(echo "$RESULT" | jq -r '.title')"
TVDB_ID="$(echo "$RESULT" | jq -r '.tvdbId')"
log "series: '$TITLE' (tvdbId=$TVDB_ID)"

# -- generate the synthetic library for the chosen title ---------------------

log "generating synthetic media for '$TITLE'"
python3 "$DEMO_DIR/generate_media.py" --title "$TITLE"

# -- root folder + add series -------------------------------------------------

ROOT_PATH="/media/tv"
EXISTING_ROOTS="$(curl_sonarr "$SONARR_URL/api/v3/rootfolder")"
if ! echo "$EXISTING_ROOTS" | jq -e --arg p "$ROOT_PATH" '.[] | select(.path == $p)' >/dev/null; then
  log "adding root folder $ROOT_PATH"
  curl_sonarr -X POST -H "Content-Type: application/json" \
    -d "{\"path\": \"$ROOT_PATH\"}" "$SONARR_URL/api/v3/rootfolder" >/dev/null
fi

SERIES_PATH="$ROOT_PATH/$TITLE"
ADD_BODY="$(echo "$RESULT" | jq \
  --arg path "$SERIES_PATH" \
  --arg rootFolderPath "$ROOT_PATH" \
  '. + {qualityProfileId: 1, rootFolderPath: $rootFolderPath, path: $path, monitored: false,
        addOptions: {searchForMissingEpisodes: false}}')"

log "adding series to Sonarr"
SERIES_RESP="$(curl_sonarr -X POST -H "Content-Type: application/json" -d "$ADD_BODY" "$SONARR_URL/api/v3/series")"
SERIES_ID="$(echo "$SERIES_RESP" | jq -r '.id // empty')"
if [ -z "$SERIES_ID" ]; then
  log "FAIL: series add failed: $SERIES_RESP"
  exit 1
fi
log "series id=$SERIES_ID"

# -- wait for the 4 files to be imported --------------------------------------
#
# Adding a series whose path already has matching files triggers Sonarr's
# own automatic disk scan as part of the add — no explicit RescanSeries
# needed on the happy path (firing one immediately after add was found to
# race Sonarr's own scan, each creating its own episodeFile row for the
# same physical file: the older one gets orphaned — unlinked from its
# episode but NOT deleted — and Impostarr's backfill, which walks every
# `/episodefile` row for a series, turned each orphan into a job with no
# resolvable episode ("error"). Waiting for the *episode* list's
# hasFile/episodeFileId mapping to stop changing (rather than a raw
# episodefile count) tolerates that churn instead of racing it; the
# leftover-orphan cleanup below is belt-and-suspenders in case duplicates
# still slip through.

trigger_rescan() {
  curl_sonarr -X POST -H "Content-Type: application/json" \
    -d "{\"name\": \"RescanSeries\", \"seriesId\": $SERIES_ID}" "$SONARR_URL/api/v3/command" >/dev/null
}

log "waiting for 4 episode files to be imported and settle"
stable_polls=0
prev_signature=""
manual_rescan_done=0
for i in $(seq 1 90); do
  episodes_json="$(curl_sonarr "$SONARR_URL/api/v3/episode?seriesId=$SERIES_ID")"
  signature="$(echo "$episodes_json" | jq -c '[.[] | select(.hasFile == true) | .episodeFileId] | sort')"
  count="$(echo "$signature" | jq 'length')"
  if [ "$count" -ge 4 ] && [ "$signature" = "$prev_signature" ]; then
    stable_polls=$((stable_polls + 1))
  else
    stable_polls=0
  fi
  prev_signature="$signature"
  [ "$stable_polls" -ge 2 ] && break
  if [ "$count" -eq 0 ] && [ "$i" -eq 15 ] && [ "$manual_rescan_done" -eq 0 ]; then
    log "  no files imported yet after 15 polls; triggering RescanSeries as a fallback"
    trigger_rescan
    manual_rescan_done=1
  fi
  sleep 2
done
if [ "$stable_polls" -lt 2 ]; then
  log "FAIL: episode files never settled at 4 imported (last signature: $prev_signature)"
  exit 1
fi
log "4 episode files imported and stable"

# Belt-and-suspenders: delete any /episodefile row for this series no
# longer referenced by any episode (see comment above).
REFERENCED_IDS="$(echo "$episodes_json" | jq -c '[.[] | select(.hasFile == true) | .episodeFileId]')"
ALL_FILE_IDS="$(curl_sonarr "$SONARR_URL/api/v3/episodefile?seriesId=$SERIES_ID" | jq -c '[.[].id]')"
ORPHAN_IDS="$(jq -n --argjson all "$ALL_FILE_IDS" --argjson ref "$REFERENCED_IDS" '$all - $ref | .[]')"
for orphan_id in $ORPHAN_IDS; do
  log "  deleting orphaned episodefile $orphan_id"
  curl_sonarr -X DELETE "$SONARR_URL/api/v3/episodefile/$orphan_id" >/dev/null
done

# -- place reference subtitles -----------------------------------------------

REFSUBS_DIR="$DEMO_DIR/volumes/config/refsubs_manual/$TVDB_ID"
mkdir -p "$REFSUBS_DIR"
cp "$DEMO_DIR"/volumes/staging/refsubs/S01E0*.srt "$REFSUBS_DIR/"
log "reference subtitles placed at $REFSUBS_DIR"

# -- render impostarr.yml -----------------------------------------------------

mkdir -p "$DEMO_DIR/volumes/config"
sed "s|__SONARR_API_KEY__|$API_KEY|" "$DEMO_DIR/impostarr.demo.yml" > "$DEMO_DIR/volumes/config/impostarr.yml"
log "rendered volumes/config/impostarr.yml"

log "seed complete: title='$TITLE' tvdbId=$TVDB_ID seriesId=$SERIES_ID"
