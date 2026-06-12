#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/benchmark-browser-testtables.sh \
    --source /path/to/commit-worktree \
    --fixtures /path/to/testtables \
    --output /path/to/testtables/tmp/<label> \
    [--fixture 'photo*.jpg'] \
    [--timeout 120] \
    [--resume]

The script runs the browser Tesseract.js/WASM engine in a fresh Node process
for every image. It covers browser OCR and layout reconstruction, but not the
DOM/Canvas preprocessing path. PDF and full-browser memory tests are separate.
EOF
}

source_root=""
fixtures_root=""
output_root=""
timeout_seconds=120
fixture_patterns=()
resume=0

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
    --fixture)
      fixture_patterns+=("$2")
      shift 2
      ;;
    --timeout)
      timeout_seconds="$2"
      shift 2
      ;;
    --resume)
      resume=1
      shift
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

for required in source_root fixtures_root output_root; do
  if [[ -z "${!required}" ]]; then
    echo "Missing --${required//_root/}" >&2
    usage >&2
    exit 2
  fi
done

source_root="$(realpath "$source_root")"
fixtures_root="$(realpath "$fixtures_root")"
mkdir -p "$output_root"
output_root="$(realpath "$output_root")"

mapfile -t fixtures < <(
  find "$fixtures_root" -maxdepth 1 -type f \
    \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) \
    -printf '%p\n' | sort
)
if [[ ${#fixture_patterns[@]} -gt 0 ]]; then
  selected_fixtures=()
  for fixture in "${fixtures[@]}"; do
    file_name="$(basename "$fixture")"
    for pattern in "${fixture_patterns[@]}"; do
      if [[ "$file_name" == $pattern ]]; then
        selected_fixtures+=("$fixture")
        break
      fi
    done
  done
  fixtures=("${selected_fixtures[@]}")
fi
if [[ ${#fixtures[@]} -eq 0 ]]; then
  echo "No supported image fixtures found in $fixtures_root" >&2
  exit 2
fi

commit="$(git -C "$source_root" rev-parse HEAD)"
subject="$(git -C "$source_root" show -s --format=%s HEAD)"
summary="$output_root/summary.tsv"
manifest="$output_root/manifest.md"

cat >"$manifest" <<EOF
# Browser OCR benchmark

- commit: \`$commit\`
- subject: $subject
- runtime: Node.js \`$(node --version)\` with Tesseract.js/WASM
- timeout per image: ${timeout_seconds}s
- fixtures: ${#fixtures[@]}
- scope: OCR worker and browser layout reconstruction
- excluded: DOM/Canvas preprocessing and browser PDF worker
EOF

if [[ $resume -ne 1 || ! -s "$summary" ]]; then
  printf 'commit\tfile\texit\twall_ms\tengine_elapsed_ms\trss_before_bytes\trss_after_bytes\n' >"$summary"
fi

for fixture in "${fixtures[@]}"; do
  file_name="$(basename "$fixture")"
  result_file="$output_root/$file_name.md"
  response_file="$output_root/$file_name.response.json"
  error_file="$output_root/$file_name.error.txt"
  if [[ $resume -eq 1 && -s "$result_file" ]]; then
    continue
  fi

  started_ms="$(date +%s%3N)"
  set +e
  (
    cd "$source_root"
    timeout --signal=TERM "${timeout_seconds}s" \
      node --import tsx scripts/benchmark-browser-ocr.ts "$fixture"
  ) >"$response_file" 2>"$error_file"
  exit_code=$?
  set -e
  wall_ms=$(( $(date +%s%3N) - started_ms ))

  python3 - \
    "$response_file" "$error_file" "$result_file" "$summary" "$commit" \
    "$file_name" "$exit_code" "$wall_ms" <<'PY'
import json
import pathlib
import sys

(
    response_path,
    error_path,
    result_path,
    summary_path,
    commit,
    file_name,
    exit_code,
    wall_ms,
) = sys.argv[1:]

response_file = pathlib.Path(response_path)
error = pathlib.Path(error_path).read_text(
    encoding="utf-8", errors="replace"
).strip()
payload = {}
try:
    payload = json.loads(response_file.read_text(encoding="utf-8"))
except Exception:
    payload = {}

text = payload.get("markdown")
if not isinstance(text, str):
    text = error or "Browser OCR produced no result."

elapsed_ms = payload.get("elapsed_ms", "")
header = f"# {wall_ms} ms (engine: {elapsed_ms or 'n/a'} ms, exit {exit_code})"
pathlib.Path(result_path).write_text(
    f"{header}\n---\n{text.rstrip()}\n",
    encoding="utf-8",
)
fields = [
    commit,
    file_name,
    exit_code,
    wall_ms,
    elapsed_ms,
    payload.get("rss_before_bytes", ""),
    payload.get("rss_after_bytes", ""),
]
with pathlib.Path(summary_path).open("a", encoding="utf-8") as output:
    output.write("\t".join(str(value).replace("\t", " ") for value in fields))
    output.write("\n")
PY

  rm -f "$response_file"
  if [[ $exit_code -eq 0 || ! -s "$error_file" ]]; then
    rm -f "$error_file"
  fi
done

printf 'Browser benchmark complete: %s\n' "$output_root"
