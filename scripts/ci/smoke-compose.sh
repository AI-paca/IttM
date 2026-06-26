#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
cd "$project_root"

project_name="${COMPOSE_PROJECT_NAME:-ittm-hw7-smoke}"
host_port="${GATEWAY_HOST_PORT:-3098}"
base_url="http://127.0.0.1:${host_port}"
fixture="$(mktemp --suffix=.png)"
use_prebuilt="${COMPOSE_SMOKE_PREBUILT:-1}"
use_ocr_stub="${COMPOSE_SMOKE_OCR_STUB:-1}"
compose_files=(-f docker-compose.yml)

if [[ "$use_prebuilt" == "1" ]]; then
  compose_files+=(-f docker-compose.smoke.yml)
fi

if [[ "$use_ocr_stub" == "1" ]]; then
  compose_files+=(-f docker-compose.ocr-smoke.yml)
fi

cleanup() {
  rm -f "$fixture"
  docker compose "${compose_files[@]}" -p "$project_name" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ "$use_prebuilt" == "1" ]]; then
  VITE_BASE_PATH=/ npm run build:web
  npm run build:server:standalone
fi

printf '%s' \
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nX0AAAAASUVORK5CYII=' \
  | base64 --decode >"$fixture"

for attempt in 1 2 3; do
  if GATEWAY_HOST_PORT="$host_port" docker compose "${compose_files[@]}" -p "$project_name" up -d --build; then
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

docker compose "${compose_files[@]}" -p "$project_name" ps
