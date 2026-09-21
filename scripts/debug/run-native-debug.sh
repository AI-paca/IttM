#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=scripts/debug/workspace-paths.sh
source "$(dirname "${BASH_SOURCE[0]}")/workspace-paths.sh"

engine="${1:-tesseract}"
case "$engine" in
  tesseract|auto|easyocr) ;;
  *) echo 'Usage: run-native-debug.sh [tesseract|auto|easyocr] [debug/fixture ...]' >&2; exit 2 ;;
esac
if [[ $# -gt 0 ]]; then shift; fi
if [[ $# -eq 0 ]]; then
  set -- debug/fixtures/SAMPLE_4k.png debug/fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf
fi

fixtures=()
for fixture in "$@"; do
  fixture="$(debug_data_path "$fixture")"
  [[ -f "$fixture" ]] || { echo "Missing fixture: $fixture" >&2; exit 2; }
  fixtures+=("$fixture")
done

cargo build --locked --release --manifest-path ocr-runtime/Cargo.toml
output="$(debug_data_path "debug/tmp/native-$engine")"
mkdir -p "$output"
status=0
for fixture in "${fixtures[@]}"; do
  name="$(basename "$fixture")"
  echo "Engine=$engine fixture=$fixture"
  # Every selected fixture is attempted; any failed conversion fails the run.
  if ! ocr-runtime/target/release/ittm-ocr convert "$fixture" \
    --engine "$engine" --output "$output/$name.md" >"$output/$name.log" 2>&1; then
    cat "$output/$name.log" >&2
    status=1
  elif [[ ! -s "$output/$name.md" ]]; then
    echo "Empty OCR result: $fixture" >&2
    status=1
  fi
done
exit "$status"
