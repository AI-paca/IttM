#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
trivy_image="${TRIVY_IMAGE:-aquasec/trivy@sha256:53570e6911c2361ebe7995228088cf83a6b9b73e7f3cdca44bd8f8f425e80fa7}"
output="${SCA_OUTPUT_DIR:-.sca}"
accepted_risk_file="$repo_root/.sca/accepted-risk.json"
cache="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy/sca-$(id -u)}"
network="${SCA_DOCKER_NETWORK:-host}"
gateway_image="${SCA_GATEWAY_IMAGE:-ittm-gateway-sca:local}"
nginx_image="${SCA_NGINX_IMAGE:-ittm-nginx-sca:local}"
ocr_image="${SCA_OCR_IMAGE:-ittm-ocr-sca:local}"
ocr_ci_image="${SCA_OCR_CI_IMAGE:-ittm-ocr-ci-sca:local}"

if [[ "$output" = /* || "$output" == *".."* ]]; then
  echo "SCA_OUTPUT_DIR must be a repository-relative path without '..'." >&2
  exit 2
fi

mkdir -p "$repo_root/$output" "$cache"
find "$repo_root/$output" -maxdepth 1 -type f -name "*.json" ! -name "accepted-risk.json" -delete

docker_args=(
  --rm
  --network "$network"
  --user "$(id -u):$(id -g)"
  -e HOME=/tmp
  -v "$cache:/trivy-cache"
  -v "$repo_root:/work"
  -w /work
)

if [[ -S /var/run/docker.sock ]]; then
  docker_args+=(
    --group-add "$(stat -c '%g' /var/run/docker.sock)"
    -v /var/run/docker.sock:/var/run/docker.sock
  )
fi

run_trivy() {
  docker run "${docker_args[@]}" "$trivy_image" --cache-dir /trivy-cache "$@"
}

verify_accepted_risk() {
  if [[ ! -f "$accepted_risk_file" ]]; then
    echo "Missing tracked accepted-risk policy: $accepted_risk_file" >&2
    return 1
  fi

  if ! command -v jq >/dev/null 2>&1; then
    echo "jq is required to verify accepted SCA risk." >&2
    return 1
  fi

  local expected="$repo_root/$output/accepted-risk-expected.txt"
  local actual="$repo_root/$output/accepted-risk-current.txt"
  local missing="$repo_root/$output/accepted-risk-missing.txt"
  local stale="$repo_root/$output/accepted-risk-stale.txt"

  jq -r '.families[] | if type == "string" then . else .package end' \
    "$accepted_risk_file" \
    | LC_ALL=C sort -u >"$expected"

  jq -r '
    .Results[]?.Vulnerabilities[]?
    | select(.FixedVersion == null or .FixedVersion == "")
    | .PkgName
  ' \
    "$repo_root/$output/gateway-vuln.json" \
    "$repo_root/$output/nginx-vuln.json" \
    "$repo_root/$output/ocr-vuln.json" \
    "$repo_root/$output/ocr-ci-vuln.json" \
    | LC_ALL=C sort -u >"$actual"

  comm -13 "$expected" "$actual" >"$missing"
  comm -23 "$expected" "$actual" >"$stale"

  if [[ -s "$missing" || -s "$stale" ]]; then
    if [[ -s "$missing" ]]; then
      echo "Unreviewed unfixed SCA package families:" >&2
      sed 's/^/  - /' "$missing" >&2
    fi
    if [[ -s "$stale" ]]; then
      echo "Accepted SCA risk no longer present in current image reports:" >&2
      sed 's/^/  - /' "$stale" >&2
    fi
    return 1
  fi
}

if [[ "${SCA_SKIP_BUILD:-0}" != "1" ]]; then
  docker build --pull --network "$network" \
    -f "$repo_root/docker/gateway.Dockerfile" \
    -t "$gateway_image" \
    "$repo_root"
  docker build --pull --network "$network" \
    -f "$repo_root/docker/nginx.Dockerfile" \
    -t "$nginx_image" \
    "$repo_root"
  docker build --pull --network "$network" \
    -f "$repo_root/docker/ocr.Dockerfile" \
    -t "$ocr_image" \
    "$repo_root/ocr"
  docker build --pull --network "$network" \
    --target test \
    --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
    -f "$repo_root/docker/ocr.Dockerfile" \
    -t "$ocr_ci_image" \
    "$repo_root/ocr"
fi

gate_status=0

if ! npm audit --json >"$repo_root/$output/npm-audit.json"; then
  gate_status=1
fi

run_trivy fs \
  --scanners vuln \
  --include-dev-deps \
  --skip-dirs "/work/$output" \
  --format json \
  /work >"$repo_root/$output/source-vuln.json"
run_trivy fs \
  --include-dev-deps \
  --skip-dirs "/work/$output" \
  --format cyclonedx \
  /work >"$repo_root/$output/source.cdx.json"

for image_spec in \
  "gateway:$gateway_image" \
  "nginx:$nginx_image" \
  "ocr:$ocr_image" \
  "ocr-ci:$ocr_ci_image"; do
  name="${image_spec%%:*}"
  image_name="${image_spec#*:}"

  run_trivy image \
    --scanners vuln \
    --format json \
    "$image_name" >"$repo_root/$output/$name-vuln.json"
  run_trivy image \
    --format cyclonedx \
    "$image_name" >"$repo_root/$output/$name.cdx.json"
done

if ! verify_accepted_risk; then
  gate_status=1
fi

if ! run_trivy fs \
  --quiet \
  --scanners vuln \
  --include-dev-deps \
  --skip-dirs "/work/$output" \
  --severity HIGH,CRITICAL \
  --format json \
  --output /tmp/source-gate.json \
  --exit-code 1 \
  /work; then
  gate_status=1
fi

for image_name in "$gateway_image" "$nginx_image" "$ocr_image" "$ocr_ci_image"; do
  if ! run_trivy image \
    --quiet \
    --scanners vuln \
    --ignore-unfixed \
    --severity MEDIUM,HIGH,CRITICAL \
    --format json \
    --output /tmp/image-gate.json \
    --exit-code 1 \
    "$image_name"; then
    gate_status=1
  fi
done

echo "SCA reports and CycloneDX SBOMs: $output/"

if [[ "$gate_status" -ne 0 ]]; then
  echo "SCA gate failed: review npm audit or fixable Trivy findings." >&2
  exit "$gate_status"
fi
