#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/debug/debug-image-engines-parallel.sh [options]

Defaults:
  - fixture block: image.png, photo_2026-06-20_15-38-09.jpg, photo_2026-06-26_19-56-47.jpg
  - engines: tesseract,easyocr,auto,browser-tesseract
  - output: debug/artifacts/parallel-reference-block
  - tmp: debug/tmp/parallel-reference-block

The script starts one independent runner per engine for the selected fixture
block, waits for all of them, then merges backend/browser summaries into one
result.csv and time.csv matrix. This is the visible parallel block test for
new or repaired image references; API engines are not implemented and are
intentionally rejected.

Options:
  --fixture GLOB             Image fixture basename; may be repeated.
  --engines CSV              tesseract,easyocr,auto,browser-tesseract.
  --fixtures DIR             Fixture directory; default debug/fixtures.
  --expected-root DIR        Reference directory; default debug/reference.
  --tmp-root DIR             Intermediate root; default debug/tmp/parallel-image.
  --output-root DIR          Artifact root; default debug/artifacts/parallel-image.
  --source DIR               Worktree source; default current directory.
  --browser-profile PROFILE  Browser profile; default browser_tesseract_dewarp.
  --pipeline-profile PROFILE Use one backend profile for every backend engine.
  --engine-profile E=P       Override backend profile for one backend engine.
  --engine-flags E=FLAGS     Override API flags for one backend engine.
  --gpu auto|on|off          Backend Docker GPU mode; default auto.
  --timeout SECONDS          Per-file timeout; default 1200.
  --resume                   Reuse per-engine markdown outputs.
EOF
}

source_root="."
fixtures_root="debug/fixtures"
expected_root="debug/reference"
tmp_root="debug/tmp/parallel-reference-block"
output_root="debug/artifacts/parallel-reference-block"
engines_csv="${OCR_DEBUG_PARALLEL_ENGINES:-tesseract,easyocr,auto,browser-tesseract}"
browser_profile="${BROWSER_OCR_PROFILE:-browser_tesseract_dewarp}"
backend_profile_args=()
backend_flag_args=()
gpu_mode="${OCR_BENCHMARK_GPU:-auto}"
timeout_seconds=1200
fixture_patterns=(
  "image.png"
  "photo_2026-06-20_15-38-09.jpg"
  "photo_2026-06-26_19-56-47.jpg"
)
using_default_fixture_patterns=1
resume_arg=()

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
    --tmp-root)
      tmp_root="$2"
      shift 2
      ;;
    --output-root)
      output_root="$2"
      shift 2
      ;;
    --engines)
      engines_csv="$2"
      shift 2
      ;;
    --fixture)
      if [[ "$using_default_fixture_patterns" -eq 1 ]]; then
        fixture_patterns=()
        using_default_fixture_patterns=0
      fi
      fixture_patterns+=("$2")
      shift 2
      ;;
    --browser-profile)
      browser_profile="$2"
      shift 2
      ;;
    --pipeline-profile)
      backend_profile_args+=(--pipeline-profile "$2")
      shift 2
      ;;
    --engine-profile)
      backend_profile_args+=(--engine-profile "$2")
      shift 2
      ;;
    --engine-flags)
      backend_flag_args+=(--engine-flags "$2")
      shift 2
      ;;
    --gpu)
      gpu_mode="$2"
      shift 2
      ;;
    --timeout)
      timeout_seconds="$2"
      shift 2
      ;;
    --resume)
      resume_arg=(--resume)
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

if [[ "$gpu_mode" != "auto" && "$gpu_mode" != "on" && "$gpu_mode" != "off" ]]; then
  echo "--gpu must be one of: auto, on, off" >&2
  exit 2
fi
if [[ ! "$timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "--timeout must be a positive integer" >&2
  exit 2
fi

source_root="$(realpath "$source_root")"
fixtures_root="$(realpath "$fixtures_root")"
expected_root="$(realpath "$expected_root")"
mkdir -p "$tmp_root" "$output_root"
tmp_root="$(realpath "$tmp_root")"
output_root="$(realpath "$output_root")"
rm -rf "$output_root"
mkdir -p "$output_root"

fixture_args=()
for pattern in "${fixture_patterns[@]}"; do
  fixture_args+=(--fixture "$pattern")
done

IFS=',' read -r -a requested_engines <<< "$engines_csv"
backend_roots=()
browser_root=""
pids=()
labels=()
status_file="$tmp_root/run-status.tsv"
: >"$status_file"

start_engine() {
  local label="$1"
  shift
  local run_root="$tmp_root/$label"
  rm -rf "$run_root"
  mkdir -p "$run_root"
  labels+=("$label")
  (
    set +e
    "$@" >"$run_root/run.log" 2>&1
    status=$?
    printf '%s\t%s\n' "$label" "$status" >>"$status_file"
    exit "$status"
  ) &
  pids+=("$!")
}

for engine in "${requested_engines[@]}"; do
  case "$engine" in
    tesseract|easyocr|auto)
      backend_roots+=("$tmp_root/$engine")
      start_engine "$engine" \
        scripts/debug/run-debug.sh \
          --source "$source_root" \
          --fixtures "$fixtures_root" \
          --expected-root "$expected_root" \
          --output "$tmp_root/$engine" \
          --engines "$engine" \
          --gpu "$gpu_mode" \
          --timeout "$timeout_seconds" \
          "${backend_profile_args[@]}" \
          "${backend_flag_args[@]}" \
          "${fixture_args[@]}" \
          "${resume_arg[@]}"
      ;;
    browser-tesseract)
      browser_root="$tmp_root/browser-tesseract"
      start_engine "$engine" \
        scripts/debug/run-browser-debug.sh \
          --source "$source_root" \
          --fixtures "$fixtures_root" \
          --output "$browser_root" \
          --timeout "$timeout_seconds" \
          --profile "$browser_profile" \
          "${fixture_args[@]}" \
          "${resume_arg[@]}"
      ;;
    api-ollama|api-openrouter|api-gemini)
      echo "Debug API engine '$engine' is scaffolded but not implemented yet." >&2
      exit 2
      ;;
    "")
      ;;
    *)
      echo "Unknown debug engine '$engine'." >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ${#pids[@]} -eq 0 ]]; then
  echo "No engines selected." >&2
  exit 2
fi

run_status=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    run_status=1
  fi
done

merged_root="$tmp_root/merged-backend"
rm -rf "$merged_root"
mkdir -p "$merged_root"

merge_table() {
  local name="$1"
  local target="$merged_root/$name"
  local wrote_header=0
  local root
  for root in "${backend_roots[@]}"; do
    if [[ ! -s "$root/$name" ]]; then
      continue
    fi
    if [[ "$wrote_header" -eq 0 ]]; then
      head -n 1 "$root/$name" >"$target"
      wrote_header=1
    fi
    tail -n +2 "$root/$name" >>"$target"
  done
}

merge_table "summary.tsv"
merge_table "comparison.csv"
merge_table "resources.tsv"

for root in "${backend_roots[@]}"; do
  [[ -d "$root" ]] || continue
  for engine_dir in "$root"/tesseract "$root"/easyocr "$root"/auto; do
    if [[ -d "$engine_dir" ]]; then
      cp -a "$engine_dir" "$merged_root/"
    fi
  done
  if [[ -d "$root/tables" ]]; then
    mkdir -p "$merged_root/tables"
    cp -a "$root/tables"/. "$merged_root/tables/" 2>/dev/null || true
  fi
done

matrix_args=(
  --benchmark-root "$merged_root"
  --expected-root "$expected_root"
  --output-root "$output_root"
  --include-auto
)
if [[ -n "$browser_root" ]]; then
  matrix_args+=(--browser-root "$browser_root")
fi
python3 scripts/debug/debug_matrix_report.py "${matrix_args[@]}"

python3 - "$output_root/result.csv" "$output_root/summary.md" <<'PY'
import csv
import pathlib
import sys

result_path = pathlib.Path(sys.argv[1])
summary_path = pathlib.Path(sys.argv[2])
with result_path.open(encoding="utf-8", newline="") as source:
    rows = list(csv.DictReader(source))

engines = [
    column.removesuffix(" %")
    for column in (rows[0].keys() if rows else [])
    if column.endswith(" %")
]
lines = [
    "# Parallel Reference Block Summary",
    "",
    "| File | " + " | ".join(engines) + " |",
    "| --- | " + " | ".join("---:" for _ in engines) + " |",
]
for row in rows:
    cells = [f"`{row['file']}`"]
    for engine in engines:
        percent = row.get(f"{engine} %", "n/a")
        gate = row.get(f"{engine} gate", "n/a")
        cells.append(f"{percent}% `{gate}`")
    lines.append("| " + " | ".join(cells) + " |")

summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote compact summary to {summary_path}")
PY

gate_status=0
python3 scripts/debug/debug_quality_gate.py \
  --result "$output_root/result.csv" \
  --strict-na \
  --required-methods "$engines_csv" || gate_status=$?

{
  printf '# Parallel Reference Block OCR Debug\n\n'
  printf -- '- source: `%s`\n' "$source_root"
  printf -- '- fixtures: `%s`\n' "$fixtures_root"
  printf -- '- references: `%s`\n' "$expected_root"
  printf -- '- engines: `%s`\n' "$engines_csv"
  printf -- '- backend profile args: `%s`\n' "${backend_profile_args[*]:-none}"
  printf -- '- backend flag args: `%s`\n' "${backend_flag_args[*]:-none}"
  printf -- '- browser profile: `%s`\n' "$browser_profile"
  printf -- '- fixture filters: `%s`\n' "${fixture_patterns[*]}"
  printf -- '- timeout: `%ss`\n' "$timeout_seconds"
  printf -- '- result matrix: `result.csv`\n'
  printf -- '- compact summary: `summary.md`\n'
  printf -- '- timing matrix: `time.csv`\n'
  printf -- '- merged backend tmp: `%s`\n\n' "$merged_root"
  printf '## Engine Status\n\n'
  printf '| Engine | Exit | Log |\n'
  printf '| --- | ---: | --- |\n'
  for label in "${labels[@]}"; do
    status="$(awk -F '\t' -v label="$label" '$1 == label {value = $2} END {print value}' "$status_file")"
    printf '| `%s` | `%s` | `%s` |\n' "$label" "${status:-missing}" "$tmp_root/$label/run.log"
  done
} >"$output_root/manifest.md"

printf 'Parallel image benchmark complete: %s\n' "$output_root"

if [[ "$run_status" -ne 0 ]]; then
  exit "$run_status"
fi
exit "$gate_status"
