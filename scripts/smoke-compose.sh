#!/usr/bin/env bash
set -euo pipefail

project_name="${COMPOSE_PROJECT_NAME:-ittm-hw5-smoke}"
host_port="${GATEWAY_HOST_PORT:-3098}"
base_url="http://127.0.0.1:${host_port}"
fixture="$(mktemp --suffix=.png)"

cleanup() {
  rm -f "$fixture"
  docker compose -p "$project_name" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf '%s' \
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nX0AAAAASUVORK5CYII=' \
  | base64 --decode >"$fixture"

for attempt in 1 2 3; do
  if GATEWAY_HOST_PORT="$host_port" docker compose -p "$project_name" up -d --build; then
    break
  fi
  if [[ "$attempt" == "3" ]]; then
    echo "Compose build failed after ${attempt} attempts." >&2
    exit 1
  fi
  echo "Compose build failed on attempt ${attempt}; retrying." >&2
  sleep $((attempt * 5))
done

for _ in $(seq 1 60); do
  if curl --fail --silent --show-error "${base_url}/api/health" >/dev/null; then
    break
  fi
  sleep 2
done

curl --fail --silent --show-error "${base_url}/api/health" >/dev/null
npm run extract -- "$fixture" \
  "--endpoint=${base_url}/api/convert/stream?engine_type=tesseract&pipeline_profile=backend_raw" \
  >/dev/null

docker compose -p "$project_name" ps
