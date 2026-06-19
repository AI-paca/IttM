#!/usr/bin/env bash
set -euo pipefail

source_root="."
fixtures_root=""
expected_root=""
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
    --expected-root)
      expected_root="$2"
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

expected_root="${expected_root:-debug/reference}"
default_fixtures_root="debug/fixtures"

has_supported_fixtures() {
  local root="$1"
  [[ -d "$root" ]] || return 1
  if find "$root" -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.png' -o -iname '*.jpg' \
       -o -iname '*.jpeg' -o -iname '*.webp' \) -print -quit | grep -q .; then
    return 0
  fi
  return 1
}

if [[ -z "$fixtures_root" ]]; then
  if has_supported_fixtures "$default_fixtures_root"; then
    fixtures_root="$default_fixtures_root"
  elif has_supported_fixtures "debug"; then
    fixtures_root="debug"
  elif [[ -d testtables ]] && has_supported_fixtures "testtables"; then
    fixtures_root="testtables"
  else
    fixtures_root="$default_fixtures_root"
  fi
fi

if [[ "$fixtures_root" == "debug" ]] &&
  ! has_supported_fixtures "$fixtures_root" &&
  [[ -d testtables ]]; then
  fixtures_root="testtables"
fi

output_root="${output_root:-debug/tmp}"

exec scripts/benchmark/benchmark-testtables.sh \
  --source "$source_root" \
  --fixtures "$fixtures_root" \
  --expected-root "$expected_root" \
  --output "$output_root" \
  "${forwarded[@]}"
