#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/debug/workspace-paths.sh
source "$(dirname "${BASH_SOURCE[0]}")/workspace-paths.sh"

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

fixtures_root="$(debug_data_path "${fixtures_root:-debug/fixtures}")"
source_root="$(debug_source_path "$source_root")"


output_root="${output_root:-debug/tmp}"

output_root="$(debug_data_path "$output_root")"
expected_root="$(debug_workspace_path "$expected_root")"

exec scripts/benchmark/benchmark-testtables.sh \
  --source "$source_root" \
  --fixtures "$fixtures_root" \
  --expected-root "$expected_root" \
  --output "$output_root" \
  "${forwarded[@]}"
