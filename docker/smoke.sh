#!/usr/bin/env bash
# End-to-end smoke test for the CPU docker image: builds it, runs it with
# throwaway volumes and a minimal config, and checks that it comes up
# healthy and serves the UI. Runnable locally and in CI.
#
# Usage: docker/smoke.sh   (run from anywhere; paths are repo-root-relative)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="impostarr:smoke"
CONTAINER_NAME="impostarr-smoke-$$"
TMPDIR_SMOKE="$(mktemp -d "${TMPDIR:-/tmp}/impostarr-smoke.XXXXXX")"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMPDIR_SMOKE"
}
trap cleanup EXIT

echo "==> Building $IMAGE_TAG"
docker build -f "$REPO_ROOT/docker/Dockerfile" -t "$IMAGE_TAG" "$REPO_ROOT"

echo "==> Preparing throwaway config/volumes at $TMPDIR_SMOKE"
mkdir -p "$TMPDIR_SMOKE/config" "$TMPDIR_SMOKE/assets" "$TMPDIR_SMOKE/models" "$TMPDIR_SMOKE/media"
cat > "$TMPDIR_SMOKE/config/impostarr.yml" <<'EOF'
sonarr: []
EOF
# Container runs as uid 1000; host uid running this script may differ (e.g.
# in CI), so open up the throwaway dirs rather than chown.
chmod -R 777 "$TMPDIR_SMOKE"

PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')"

echo "==> Starting container on port $PORT"
docker run -d --name "$CONTAINER_NAME" \
  -p "${PORT}:8484" \
  -v "$TMPDIR_SMOKE/config:/config" \
  -v "$TMPDIR_SMOKE/assets:/assets" \
  -v "$TMPDIR_SMOKE/models:/models" \
  -v "$TMPDIR_SMOKE/media:/media" \
  "$IMAGE_TAG" >/dev/null

echo "==> Polling /api/v1/healthz (up to 60s)"
healthy=0
for _ in $(seq 1 60); do
  if code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/api/v1/healthz" 2>/dev/null)" && [ "$code" = "200" ]; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "$healthy" -ne 1 ]; then
  echo "FAIL: healthz did not return 200 within 60s" >&2
  docker logs "$CONTAINER_NAME" >&2 || true
  exit 1
fi
echo "    healthz OK"

echo "==> Checking / serves the UI"
body="$(curl -fsS "http://localhost:${PORT}/")"
if ! printf '%s' "$body" | grep -qE 'Impostarr|<div id="root">'; then
  echo "FAIL: / did not contain expected UI markers" >&2
  echo "$body" >&2
  exit 1
fi
echo "    UI OK"

echo "==> Smoke test passed"
