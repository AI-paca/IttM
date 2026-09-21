#!/usr/bin/env bash
# Shared by engine runners. Resolve symlinks before accepting an input/output.
DEBUG_WORKSPACE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$DEBUG_WORKSPACE"

debug_workspace_path() {
  local value="$1" resolved
  resolved="$(realpath -m -- "$value")" || return 2
  case "$resolved" in
    "$DEBUG_WORKSPACE") printf '.\n' ;;
    "$DEBUG_WORKSPACE"/*) printf '%s\n' "${resolved#"$DEBUG_WORKSPACE"/}" ;;
    *) echo "Path leaves the current workspace: $value" >&2; return 2 ;;
  esac
}

debug_data_path() {
  local value
  value="$(debug_workspace_path "$1")" || return 2
  case "$value" in
    debug|debug/*) printf '%s\n' "$value" ;;
    *) echo "Debug input/output must be inside debug/: $1" >&2; return 2 ;;
  esac
}

debug_source_path() {
  local value
  value="$(debug_workspace_path "$1")" || return 2
  if [[ "$value" != '.' ]]; then
    echo "Engine runners use the current workspace; --source must be ." >&2
    return 2
  fi
  printf '.\n'
}
