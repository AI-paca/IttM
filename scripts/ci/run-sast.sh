#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${SEMGREP_IMAGE:-semgrep/semgrep@sha256:c180f0c93a17b420c0af5006214a29d3c747c5459c732b740191adf657dd0068}"
json_output="${SAST_OUTPUT:-.sast/semgrep.json}"
sarif_output="${SAST_SARIF_OUTPUT:-.sast/semgrep.sarif}"

validate_repo_relative_path() {
  local name="$1"
  local value="$2"
  if [[ "$value" = /* || "$value" == *".."* ]]; then
    echo "$name must be a repository-relative path without '..'." >&2
    exit 2
  fi
}

validate_repo_relative_path "SAST_OUTPUT" "$json_output"
validate_repo_relative_path "SAST_SARIF_OUTPUT" "$sarif_output"

mkdir -p "$repo_root/$(dirname "$json_output")" "$repo_root/$(dirname "$sarif_output")"

if [[ -n "${SAST_TARGETS:-}" ]]; then
  # shellcheck disable=SC2206
  targets=($SAST_TARGETS)
else
  targets=(
    docker-compose.yml
    .github/workflows
    gateway/nginx.conf
    gateway/src
    web/src
    edge/cloudflare-worker.ts
    ocr/app
    scripts/ci
    scripts/runtime
    scripts/cli
    docker
    server.ts
  )
fi

set +e
docker run --rm \
  --network none \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e SEMGREP_ENABLE_VERSION_CHECK=0 \
  -v "$repo_root:/src" \
  -w /src \
  "$image" \
  semgrep scan \
    --no-git-ignore \
    --config .semgrep/sast.yml \
    --metrics=off \
    --error \
    --json \
    "${targets[@]}" \
  >"$repo_root/$json_output"
status=$?
set -e

docker run --rm \
  --network none \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e SEMGREP_ENABLE_VERSION_CHECK=0 \
  -v "$repo_root:/src" \
  -w /src \
  "$image" \
  semgrep scan \
    --no-git-ignore \
    --config .semgrep/sast.yml \
    --metrics=off \
    --no-error \
    --sarif \
    "${targets[@]}" \
  >"$repo_root/$sarif_output" || true

node "$repo_root/scripts/ci/summarize-sast.mjs" "$json_output" || true

echo "SAST JSON: $json_output"
echo "SAST SARIF: $sarif_output"
exit "$status"
