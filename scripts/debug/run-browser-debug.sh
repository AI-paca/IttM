#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/debug/workspace-paths.sh
source "$(dirname "${BASH_SOURCE[0]}")/workspace-paths.sh"

source_root="."
fixtures_root=""
default_fixtures_root="debug/fixtures"
output_root=""
forwarded=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      source_root="$2"
      shift 2
      ;;
    --fixtures)
      fixtures_root="$2"
      shift 2
      ;;
    --output)
      output_root="$2"
      shift 2
      ;;
    *)
      forwarded+=("$1")
      shift
      ;;
  esac
done

has_supported_browser_fixtures() {
  local root="$1"
  [[ -d "$root" ]] || return 1
  find "$root" -maxdepth 1 -type f \
    \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) \
    -print -quit | grep -q .
}

fixtures_root="$(debug_data_path "${fixtures_root:-debug/fixtures}")"
source_root="$(debug_source_path "$source_root")"


output_root="${output_root:-debug/tmp/browser-tesseract}"

output_root="$(debug_data_path "$output_root")"

exec scripts/benchmark/benchmark-browser-testtables.sh \
  --source "$source_root" \
  --fixtures "$fixtures_root" \
  --output "$output_root" \
  "${forwarded[@]}"
