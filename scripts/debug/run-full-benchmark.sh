#!/usr/bin/env bash
# Run the full OCR debug benchmark and archive outputs under one version suffix.

set -uo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/debug/run-full-benchmark.sh --version N

Runs the standard three-phase OCR benchmark:
  1. full-pdf-native
  2. full-pdf-as-png
  3. image-fixtures

Outputs are moved from debug/{artifacts,tmp}/<suite> to
debug/{artifacts,tmp}/<suite>-vN after the run.
EOF
}

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

version=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      version="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

version="${version#v}"
version="${version#V}"
if [[ ! "$version" =~ ^[1-9][0-9]*$ ]]; then
  echo "--version must be a positive integer, for example: --version 12" >&2
  exit 2
fi

suffix="v$version"
tag="schedule-$suffix"

ENGINE_FLAGS_TESS="tesseract=structural_output:markdown"
ENGINE_FLAGS_EASY="easyocr=structural_output:markdown"
ENGINES="tesseract,easyocr,browser-tesseract"
MAX_PAGES="5"
ADOBE_LIMIT="Adobe Scan Oct 26, 2022 (1).pdf=5"
TIMEOUT=1200
EXPECTED_ROOT="debug/reference"

stamp() { date '+%Y-%m-%dT%H:%M:%S%:z'; }

log() { echo "[$tag] $(stamp) $*"; }

run_phase1() {
  log "PHASE 1/3 - PDF native (no raster)"
  scripts/debug/debug-all.sh \
    --engines "$ENGINES" \
    --fixtures debug/fixtures \
    --expected-root "$EXPECTED_ROOT" \
    --tmp-root debug/tmp/full-pdf-native \
    --output-root debug/artifacts/full-pdf-native \
    --timeout "$TIMEOUT" \
    --max-pages "$MAX_PAGES" \
    --fixture-max-pages "$ADOBE_LIMIT" \
    --no-pdf-raster \
    --fixture '*.pdf' \
    --engine-flags "$ENGINE_FLAGS_TESS" \
    --engine-flags "$ENGINE_FLAGS_EASY"
}

run_phase2() {
  log "PHASE 2/3 - PDF rasterized as PNG"
  scripts/debug/debug-all.sh \
    --engines "$ENGINES" \
    --fixtures debug/fixtures \
    --expected-root "$EXPECTED_ROOT" \
    --tmp-root debug/tmp/full-pdf-as-png \
    --output-root debug/artifacts/full-pdf-as-png \
    --timeout "$TIMEOUT" \
    --max-pages "$MAX_PAGES" \
    --fixture-max-pages "$ADOBE_LIMIT" \
    --pdf-raster-only \
    --pdf-raster-max-pages "$MAX_PAGES" \
    --engine-flags "$ENGINE_FLAGS_TESS" \
    --engine-flags "$ENGINE_FLAGS_EASY"
}

run_phase3() {
  log "PHASE 3/3 - real image fixtures (png/jpg)"
  scripts/debug/debug-all.sh \
    --engines "$ENGINES" \
    --fixtures debug/fixtures \
    --expected-root "$EXPECTED_ROOT" \
    --tmp-root debug/tmp/image-fixtures \
    --output-root debug/artifacts/image-fixtures \
    --timeout "$TIMEOUT" \
    --no-pdf-raster \
    --engine-flags "$ENGINE_FLAGS_TESS" \
    --engine-flags "$ENGINE_FLAGS_EASY"
}

rename_to_version() {
  log "renaming fresh results to -$suffix..."
  local pairs=(
    "debug/artifacts/full-pdf-native:debug/artifacts/full-pdf-native-$suffix"
    "debug/tmp/full-pdf-native:debug/tmp/full-pdf-native-$suffix"
    "debug/artifacts/full-pdf-as-png:debug/artifacts/full-pdf-as-png-$suffix"
    "debug/tmp/full-pdf-as-png:debug/tmp/full-pdf-as-png-$suffix"
    "debug/artifacts/image-fixtures:debug/artifacts/image-fixtures-$suffix"
    "debug/tmp/image-fixtures:debug/tmp/image-fixtures-$suffix"
  )
  for pair in "${pairs[@]}"; do
    src="${pair%%:*}"
    dst="${pair##*:}"
    if [[ -e "$dst" ]]; then
      rm -rf "$dst"
    fi
    if [[ -e "$src" ]]; then
      mv "$src" "$dst"
      log "  $src -> $dst"
    fi
  done
}

log "scheduled full $suffix benchmark to start now (waiting 0s)..."
log "delay elapsed. starting full benchmark now."
echo "Full fresh benchmark suite (3 passes, from scratch, no --resume)"
echo "engines: $ENGINES   timeout: ${TIMEOUT}s   max-pages: $MAX_PAGES   dpi: 300"
echo "engine-flags: $ENGINE_FLAGS_TESS ; $ENGINE_FLAGS_EASY"
echo "started: $(stamp)"
echo

start_s="$(date +%s)"

run_phase1; p1=$?
run_phase2; p2=$?
run_phase3; p3=$?

end_s="$(date +%s)"
elapsed=$((end_s - start_s))
log "benchmark finished (phase exits p1=$p1 p2=$p2 p3=$p3) after ${elapsed}s"

rename_to_version
log "${suffix^^} DONE (benchmark + rename in ${elapsed}s)"
log "results:"
log "  debug/artifacts/full-pdf-native-$suffix/result.csv"
log "  debug/artifacts/full-pdf-as-png-$suffix/result.csv"
log "  debug/artifacts/image-fixtures-$suffix/result.csv"

if [[ "$p1" -ne 0 || "$p2" -ne 0 || "$p3" -ne 0 ]]; then
  exit 1
fi
