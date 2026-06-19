#!/usr/bin/env bash
set -euo pipefail

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

if [[ -z "$fixtures_root" ]]; then
  if has_supported_browser_fixtures "$default_fixtures_root"; then
    fixtures_root="$default_fixtures_root"
  elif has_supported_browser_fixtures "debug"; then
    fixtures_root="debug"
  elif [[ -d testtables ]] && has_supported_browser_fixtures "testtables"; then
    fixtures_root="testtables"
  else
    fixtures_root="$default_fixtures_root"
  fi
fi

if [[ "$fixtures_root" == "debug" ]] &&
  ! has_supported_browser_fixtures "$fixtures_root" &&
  [[ -d testtables ]]; then
  fixtures_root="testtables"
fi

output_root="${output_root:-debug/tmp/browser-tesseract}"

exec scripts/benchmark/benchmark-browser-testtables.sh \
  --source "$source_root" \
  --fixtures "$fixtures_root" \
  --output "$output_root" \
  "${forwarded[@]}"
