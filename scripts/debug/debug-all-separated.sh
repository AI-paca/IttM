#!/usr/bin/env bash
set -euo pipefail

STAGES=(
  preprocess
  geometry
  topology
  find-object
  separate-block
  ocr-blocks
  get-segment
  generate-object
)

declare -A STAGE_DIR=(
  [preprocess]="00-preprocess"
  [geometry]="01-geometry"
  [topology]="02-topology"
  [find-object]="03-find-object"
  [separate-block]="04-separate-block"
  [ocr-blocks]="05-ocr-blocks"
  [get-segment]="06-get-segment"
  [generate-object]="07-generate-object"
)

usage() {
  cat <<'EOF'
Usage:
  scripts/debug/debug-all-separated.sh [options]

Run every debug boundary explicitly:
  preprocess -> geometry -> topology -> find-object -> separate-block
  -> ocr-blocks -> get-segment -> generate-object

Selection:
  --fixtures DIR              Fixture directory (default: debug/fixtures).
  --fixture GLOB              Select basename glob; repeatable.
  --source FILE               Select an explicit raster source; repeatable.
                              Without --fixture, skip the default fixture scan.
  --run-id ID                 New run id.
  --output-root DIR           Run parent (default: debug/tmp/debug-all-separated).
  --resume RUN_DIR            Continue an existing run using its items.tsv.

Stage range:
  --from-stage STAGE          First stage to execute.
  --to-stage STAGE            Last stage to execute.
  --only-stage STAGE          Equivalent to --from-stage STAGE --to-stage STAGE.
  --list-stages               Print canonical stage names.

Input replacement:
  --input STAGE=PATH          Replace the input consumed by STAGE; repeatable.
                              PATH may contain {item}. For corpus injection,
                              PATH/<item-id> is selected automatically.

Examples:
  # Stop after object extraction.
  scripts/debug/debug-all-separated.sh --fixture 'image (10).png' \
    --run-id image10-find --to-stage find-object

  # Continue the same run with the next stage.
  scripts/debug/debug-all-separated.sh \
    --resume debug/tmp/debug-all-separated/image10-find \
    --from-stage separate-block --to-stage separate-block

  # Feed separate-block a known-good find-object artifact.
  scripts/debug/debug-all-separated.sh \
    --resume debug/tmp/debug-all-separated/image10-find \
    --only-stage separate-block \
    --input separate-block=/path/to/good-run/items/{item}/03-find-object

Execution:
  --runtime RUNTIME          legacy-python (reference only), rust-native, or
                             rust-wasm-node (WASM browser adapter under Node).
  --continue-on-failure       Intentionally run downstream on failed input.
                              Without it, one file stops at its first BAD.
  --diagnostic-batch          Run every stage for every file even after BAD,
                              preserve FAILED results, and exit successfully
                              after the complete corpus scan.
  --resume-from-last-good     Reuse COMPLETE stages per file, replace its first
                              failed stage, then continue only on success.
  --replace-stage             Replace selected stage directories if present.
  --preprocess-step STEP      none or projector_slide_dewarp
                              (default: projector_slide_dewarp, as production).
  --preprocess-override GLOB=STEP
                              Override preprocessing per source; repeatable.
  --reference-root DIR        Exact text references (default: debug/reference).
  --languages CSV             Base Tesseract ids (default: rus,eng).
  --tessdata DIR              Explicit tessdata directory.
  --tesseract-workers N       Default: 4.
  --tesseract-psm 4|6         Default: 6.
  --notify-terminal           Bell/OSC notification plus notification.txt.
EOF
}

fixtures_root="debug/fixtures"
reference_root="debug/reference"
output_root="debug/tmp/debug-all-separated"
run_id=""
resume_dir=""
from_stage="preprocess"
to_stage="generate-object"
preprocess_step="projector_slide_dewarp"
preprocess_overrides=()
languages="${DEBUG_TESSERACT_LANGUAGES:-rus,eng}"
tessdata=""
tesseract_workers=4
tesseract_psm=6
continue_on_failure=0
diagnostic_batch=0
resume_from_last_good=0
replace_stage=0
notify_terminal=0
runtime="legacy-python"
list_stages=0
fixture_patterns=()
explicit_sources=()
declare -A INPUT_OVERRIDES=()

normalize_stage() {
  case "$1" in
    preprocess|geometry|topology|find-object|separate-block|ocr-blocks|get-segment|generate-object)
      printf '%s\n' "$1"
      ;;
    objects)
      printf '%s\n' "find-object"
      ;;
    blocks)
      printf '%s\n' "separate-block"
      ;;
    *)
      echo "Unknown stage: $1" >&2
      return 2
      ;;
  esac
}

stage_index() {
  local requested
  requested="$(normalize_stage "$1")"
  local index
  for index in "${!STAGES[@]}"; do
    if [[ "${STAGES[$index]}" == "$requested" ]]; then
      printf '%s\n' "$index"
      return 0
    fi
  done
  return 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fixtures)
      fixtures_root="$2"
      shift 2
      ;;
    --fixture)
      fixture_patterns+=("$2")
      shift 2
      ;;
    --source)
      explicit_sources+=("$2")
      shift 2
      ;;
    --run-id)
      run_id="$2"
      shift 2
      ;;
    --output-root)
      output_root="$2"
      shift 2
      ;;
    --resume)
      resume_dir="$2"
      shift 2
      ;;
    --from-stage)
      from_stage="$(normalize_stage "$2")"
      shift 2
      ;;
    --to-stage)
      to_stage="$(normalize_stage "$2")"
      shift 2
      ;;
    --only-stage)
      from_stage="$(normalize_stage "$2")"
      to_stage="$from_stage"
      shift 2
      ;;
    --input)
      if [[ "$2" != *=* ]]; then
        echo "--input requires STAGE=PATH" >&2
        exit 2
      fi
      input_stage="$(normalize_stage "${2%%=*}")"
      INPUT_OVERRIDES["$input_stage"]="${2#*=}"
      shift 2
      ;;
    --continue-on-failure)
      continue_on_failure=1
      shift
      ;;
    --diagnostic-batch)
      diagnostic_batch=1
      continue_on_failure=1
      shift
      ;;
    --resume-from-last-good)
      resume_from_last_good=1
      shift
      ;;
    --replace-stage)
      replace_stage=1
      shift
      ;;
    --preprocess-step)
      preprocess_step="$2"
      shift 2
      ;;
    --preprocess-override)
      preprocess_overrides+=("$2")
      shift 2
      ;;
    --reference-root)
      reference_root="$2"
      shift 2
      ;;
    --languages)
      languages="$2"
      shift 2
      ;;
    --tessdata)
      tessdata="$2"
      shift 2
      ;;
    --tesseract-workers)
      tesseract_workers="$2"
      shift 2
      ;;
    --tesseract-psm)
      tesseract_psm="$2"
      shift 2
      ;;
    --notify-terminal)
      notify_terminal=1
      shift
      ;;
    --runtime)
      runtime="$2"
      shift 2
      ;;
    --list-stages)
      list_stages=1
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

case "$runtime" in
  legacy-python|rust-native|rust-wasm-node) ;;
  *)
    echo "--runtime must be legacy-python, rust-native, or rust-wasm-node" >&2
    exit 2
    ;;
esac

if [[ "$list_stages" -eq 1 ]]; then
  printf '%s\n' "${STAGES[@]}"
  exit 0
fi

from_index="$(stage_index "$from_stage")"
to_index="$(stage_index "$to_stage")"
if (( from_index > to_index )); then
  echo "--from-stage must not follow --to-stage" >&2
  exit 2
fi
valid_preprocess_step() {
  [[ "$1" == "none" || "$1" == "projector_slide_dewarp" ]]
}

if ! valid_preprocess_step "$preprocess_step"; then
  echo "--preprocess-step must be none or projector_slide_dewarp" >&2
  exit 2
fi
for override in "${preprocess_overrides[@]}"; do
  if [[ "$override" != *=* || -z "${override%%=*}" ]]; then
    echo "--preprocess-override requires GLOB=STEP: $override" >&2
    exit 2
  fi
  override_step="${override#*=}"
  if ! valid_preprocess_step "$override_step"; then
    echo "invalid preprocessing override step: $override_step" >&2
    exit 2
  fi
done
if [[ ! "$tesseract_workers" =~ ^[1-9][0-9]*$ ]]; then
  echo "--tesseract-workers must be a positive integer" >&2
  exit 2
fi
if [[ "$tesseract_psm" != "4" && "$tesseract_psm" != "6" ]]; then
  echo "--tesseract-psm must be 4 or 6" >&2
  exit 2
fi

ocr_index="$(stage_index ocr-blocks)"
if (( from_index <= ocr_index && ocr_index <= to_index )) &&
  [[ "$runtime" != "rust-wasm-node" ]]; then
  if ! command -v tesseract >/dev/null 2>&1; then
    echo "Tesseract is required when the selected range includes ocr-blocks" >&2
    exit 2
  fi
  if [[ -n "$tessdata" ]]; then
    if [[ ! -d "$tessdata" ]]; then
      echo "Tessdata directory does not exist: $tessdata" >&2
      exit 2
    fi
    tessdata="$(realpath "$tessdata")"
  fi
  declare -A available_tesseract_languages=()
  while IFS= read -r language; do
    if [[ "$language" =~ ^[A-Za-z0-9_]+$ ]]; then
      available_tesseract_languages["$language"]=1
    fi
  done < <(
    if [[ -n "$tessdata" ]]; then
      tesseract --tessdata-dir "$tessdata" --list-langs 2>/dev/null
    else
      tesseract --list-langs 2>/dev/null
    fi
  )
  IFS=',' read -r -a requested_tesseract_languages <<< "$languages"
  missing_tesseract_languages=()
  for language in "${requested_tesseract_languages[@]}"; do
    if [[ -z "$language" || -z "${available_tesseract_languages[$language]:-}" ]]; then
      missing_tesseract_languages+=("${language:-<empty>}")
    fi
  done
  if [[ ${#missing_tesseract_languages[@]} -gt 0 ]]; then
    printf 'Missing Tesseract languages: %s\n' \
      "$(IFS=,; echo "${missing_tesseract_languages[*]}")" >&2
    exit 2
  fi
fi

matches_patterns() {
  local basename="$1"
  local pattern
  if [[ ${#fixture_patterns[@]} -eq 0 ]]; then
    return 0
  fi
  for pattern in "${fixture_patterns[@]}"; do
    if [[ "$basename" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

safe_item_id() {
  local source="$1"
  local basename stem digest
  basename="$(basename "$source")"
  stem="${basename%.*}"
  stem="$(printf '%s' "$stem" | sed -E 's/[^A-Za-z0-9._-]+/-/g; s/^-+//; s/-+$//')"
  digest="$(printf '%s' "$basename" | sha256sum | cut -c1-12)"
  printf '%s-%s\n' "$digest" "${stem:-item}"
}

declare -a ITEM_IDS=()
declare -A ITEM_SOURCE=()
declare -A ITEM_REFERENCE=()

if [[ -n "$resume_dir" ]]; then
  run_dir="$(realpath "$resume_dir")"
  if [[ ! -f "$run_dir/items.tsv" ]]; then
    echo "Resume run has no items.tsv: $run_dir" >&2
    exit 2
  fi
  while IFS=$'\t' read -r item_id source reference; do
    [[ "$item_id" == "item_id" || -z "$item_id" ]] && continue
    if matches_patterns "$(basename "$source")"; then
      ITEM_IDS+=("$item_id")
      ITEM_SOURCE["$item_id"]="$source"
      ITEM_REFERENCE["$item_id"]="$reference"
    fi
  done < "$run_dir/items.tsv"
else
  if [[ -z "$run_id" ]]; then
    run_id="debug-separated-$(date +%Y%m%d-%H%M%S)"
  fi
  if [[ ! "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    echo "Unsafe --run-id: $run_id" >&2
    exit 2
  fi
  run_dir="$(realpath -m "$output_root/$run_id")"
  if [[ -e "$run_dir" ]]; then
    echo "Run already exists; use --resume: $run_dir" >&2
    exit 2
  fi
  mkdir -p "$run_dir/items"

  selected_sources=()
  if [[ -d "$fixtures_root" ]] &&
    (( ${#explicit_sources[@]} == 0 || ${#fixture_patterns[@]} > 0 )); then
    while IFS= read -r source; do
      if matches_patterns "$(basename "$source")"; then
        selected_sources+=("$source")
      fi
    done < <(
      find "$fixtures_root" -maxdepth 1 -type f \
        \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) \
        -print | sort
    )
  fi
  selected_sources+=("${explicit_sources[@]}")
  if [[ ${#selected_sources[@]} -eq 0 ]]; then
    echo "No raster fixtures selected" >&2
    exit 2
  fi

  printf 'item_id\tsource\treference\n' > "$run_dir/items.tsv"
  declare -A seen_items=()
  for source in "${selected_sources[@]}"; do
    source="$(realpath "$source")"
    item_id="$(safe_item_id "$source")"
    [[ -n "${seen_items[$item_id]:-}" ]] && continue
    seen_items["$item_id"]=1
    reference="$(realpath -m "$reference_root/$(basename "$source").md")"
    ITEM_IDS+=("$item_id")
    ITEM_SOURCE["$item_id"]="$source"
    ITEM_REFERENCE["$item_id"]="$reference"
    printf '%s\t%s\t%s\n' "$item_id" "$source" "$reference" >> "$run_dir/items.tsv"
  done

  commit="$(git rev-parse HEAD)"
  dirty="false"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    dirty="true"
  fi
  python3 -c '
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
  "schema": "debug-separated-run-v3",
  "commit": sys.argv[2],
  "dirty": sys.argv[3] == "true",
  "stages": sys.argv[4].split(","),
  "runtime": sys.argv[5],
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
' "$run_dir/run.json" "$commit" "$dirty" "$(IFS=,; echo "${STAGES[*]}")" "$runtime"
fi

if [[ ${#ITEM_IDS[@]} -eq 0 ]]; then
  echo "No resume items selected" >&2
  exit 2
fi

commit="$(git rev-parse HEAD)"
dirty="false"
if ! git diff --quiet || ! git diff --cached --quiet; then
  dirty="true"
fi
python3 -c '
import datetime, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
record = {
    "schema": "debug-separated-invocation-v1",
    "timestamp": datetime.datetime.now().astimezone().isoformat(),
    "commit": sys.argv[2],
    "dirty": sys.argv[3] == "true",
    "resume": sys.argv[4] == "true",
    "from_stage": sys.argv[5],
    "to_stage": sys.argv[6],
    "preprocess_step": sys.argv[7],
    "languages": sys.argv[8].split(","),
    "tessdata": sys.argv[9] or None,
    "tesseract_workers": int(sys.argv[10]),
    "tesseract_psm": int(sys.argv[11]),
    "continue_on_failure": sys.argv[12] == "true",
    "diagnostic_batch": sys.argv[13] == "true",
    "resume_from_last_good": sys.argv[14] == "true",
}
with path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\n")
' "$run_dir/invocations.jsonl" "$commit" "$dirty" \
  "$([[ -n "$resume_dir" ]] && echo true || echo false)" \
  "$from_stage" "$to_stage" "$preprocess_step" "$languages" "$tessdata" \
  "$tesseract_workers" "$tesseract_psm" \
  "$([[ "$continue_on_failure" -eq 1 ]] && echo true || echo false)" \
  "$([[ "$diagnostic_batch" -eq 1 ]] && echo true || echo false)" \
  "$([[ "$resume_from_last_good" -eq 1 ]] && echo true || echo false)"

mkdir -p "$run_dir/logs"
status_file="$run_dir/status.tsv"
if [[ ! -f "$status_file" ]]; then
  printf 'timestamp\titem_id\tstage\tstatus\tinput\tinjected\texit_code\n' > "$status_file"
fi

resolve_override() {
  local stage="$1"
  local item_id="$2"
  local raw="${INPUT_OVERRIDES[$stage]:-}"
  if [[ -z "$raw" ]]; then
    return 1
  fi
  raw="${raw//\{item\}/$item_id}"
  if [[ -d "$raw/$item_id" ]]; then
    raw="$raw/$item_id"
  fi
  realpath "$raw"
}

normalize_stage_input() {
  local stage="$1"
  local input="$2"
  case "$stage" in
    geometry)
      if [[ -d "$input" && -f "$input/raster.png" ]]; then
        printf '%s\n' "$input/raster.png"
      else
        printf '%s\n' "$input"
      fi
      ;;
    topology)
      if [[ -d "$input/${STAGE_DIR[geometry]}" ]]; then
        printf '%s\n' "$input/${STAGE_DIR[geometry]}"
      else
        printf '%s\n' "$input"
      fi
      ;;
    find-object)
      if [[ -d "$input/${STAGE_DIR[geometry]}" && -d "$input/${STAGE_DIR[topology]}" ]]; then
        printf '%s\n' "$input"
      elif [[ "$(basename "$input")" == "${STAGE_DIR[topology]}" ]]; then
        printf '%s\n' "$(dirname "$input")"
      else
        printf '%s\n' "$input"
      fi
      ;;
    separate-block)
      if [[ -d "$input/${STAGE_DIR[find-object]}" ]]; then
        printf '%s\n' "$input/${STAGE_DIR[find-object]}"
      else
        printf '%s\n' "$input"
      fi
      ;;
    ocr-blocks)
      if [[ -d "$input/${STAGE_DIR[separate-block]}" ]]; then
        printf '%s\n' "$input/${STAGE_DIR[separate-block]}"
      else
        printf '%s\n' "$input"
      fi
      ;;
    get-segment)
      if [[ -d "$input/${STAGE_DIR[ocr-blocks]}" ]]; then
        printf '%s\n' "$input/${STAGE_DIR[ocr-blocks]}"
      else
        printf '%s\n' "$input"
      fi
      ;;
    generate-object)
      if [[ -d "$input/${STAGE_DIR[get-segment]}" ]]; then
        printf '%s\n' "$input/${STAGE_DIR[get-segment]}"
      else
        printf '%s\n' "$input"
      fi
      ;;
    *)
      printf '%s\n' "$input"
      ;;
  esac
}

default_input() {
  local stage="$1"
  local item_id="$2"
  local item_dir="$run_dir/items/$item_id"
  case "$stage" in
    preprocess)
      printf '%s\n' "${ITEM_SOURCE[$item_id]}"
      ;;
    geometry)
      printf '%s\n' "$item_dir/${STAGE_DIR[preprocess]}/raster.png"
      ;;
    topology)
      printf '%s\n' "$item_dir/${STAGE_DIR[geometry]}"
      ;;
    find-object)
      printf '%s\n' "$item_dir"
      ;;
    separate-block)
      printf '%s\n' "$item_dir/${STAGE_DIR[find-object]}"
      ;;
    ocr-blocks)
      printf '%s\n' "$item_dir/${STAGE_DIR[separate-block]}"
      ;;
    get-segment)
      printf '%s\n' "$item_dir/${STAGE_DIR[ocr-blocks]}"
      ;;
    generate-object)
      printf '%s\n' "$item_dir/${STAGE_DIR[get-segment]}"
      ;;
  esac
}

write_input_manifest() {
  local path="$1"
  local item_id="$2"
  local stage="$3"
  local input="$4"
  local injected="$5"
  local commit
  commit="$(git rev-parse HEAD)"
  python3 -c '
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
  "schema": "debug-stage-input-v1",
  "item_id": sys.argv[2],
  "stage": sys.argv[3],
  "input": sys.argv[4],
  "injected": sys.argv[5] == "true",
  "commit": sys.argv[6],
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
' "$path" "$item_id" "$stage" "$input" "$injected" "$commit"
}

preprocess_step_for() {
  local source="$1"
  local basename selected override pattern
  basename="$(basename "$source")"
  selected="$preprocess_step"
  for override in "${preprocess_overrides[@]}"; do
    pattern="${override%%=*}"
    if [[ "$source" == $pattern || "$basename" == $pattern ]]; then
      selected="${override#*=}"
    fi
  done
  printf '%s\n' "$selected"
}

run_preprocess() {
  local input="$1"
  local output="$2"
  local work="$3"
  local step="$4"
  python3 scripts/debug/debug_stage0_preprocess.py \
    "$input" \
    --output "$work" \
    --run-id stage \
    --step "$step"
  mv "$work/stage/00-preprocess" "$output"
}

run_geometry() {
  local input="$1"
  local output="$2"
  local work="$3"
  python3 scripts/debug/debug_geometry.py \
    "$input" \
    --output "$work" \
    --run-id stage
  mv "$work/stage/01-geometry" "$output"
}

run_topology() {
  local input="$1"
  local output="$2"
  python3 scripts/debug/annotate_pixel_partition_topology.py \
    "$input" \
    --output-dir "$output"
}

run_find_object() {
  local input_root="$1"
  local output="$2"
  local geometry_dir="$input_root/${STAGE_DIR[geometry]}"
  local topology_dir="$input_root/${STAGE_DIR[topology]}"
  if [[ ! -d "$geometry_dir" || ! -d "$topology_dir" ]]; then
    echo "find-object input must contain ${STAGE_DIR[geometry]} and ${STAGE_DIR[topology]}: $input_root" >&2
    return 2
  fi
  mkdir -p "$output/_raw"
  python3 scripts/debug/extract_recursive_topology_objects.py \
    "$geometry_dir" \
    --topology-dir "$topology_dir" \
    --output-dir "$output/_raw" \
    --replace || return $?
  python3 scripts/debug/debug_object_stage.py \
    --stage find-object \
    --geometry-dir "$geometry_dir" \
    --objects-dir "$output/_raw" \
    --output "$output"
}

run_object_stage() {
  local stage="$1"
  local input="$2"
  local output="$3"
  local args=(
    python3 scripts/debug/debug_object_stage.py
    --stage "$stage"
    --input "$input"
    --output "$output"
  )
  if [[ "$stage" == "ocr-blocks" ]]; then
    args+=(
      --languages "$languages"
      --tesseract-workers "$tesseract_workers"
      --tesseract-psm "$tesseract_psm"
    )
    if [[ -n "$tessdata" ]]; then
      args+=(--tessdata "$tessdata")
    fi
  fi
  if [[ "$stage" == "generate-object" && -f "$4" ]]; then
    args+=(--reference "$4")
  fi
  "${args[@]}"
}

overall_failed=0
  if [[ "$runtime" != "legacy-python" ]]; then
  if [[ ${#INPUT_OVERRIDES[@]} -ne 0 ]]; then
    echo "$runtime does not support stage input injection yet; use legacy-python for injected artifacts." >&2
    exit 2
  fi
  if [[ "$from_stage" != "preprocess" && "$resume_from_last_good" -ne 1 ]]; then
    echo "$runtime can start after preprocess only with --resume-from-last-good; an incomplete item is recomputed from its raster." >&2
    exit 2
  fi
  if [[ "$runtime" == "rust-wasm-node" && "$to_stage" != "generate-object" ]]; then
    echo "rust-wasm-node currently supports only a complete run; use rust-native for planning-only corpus diagnostics." >&2
    exit 2
  fi
  if [[ "$runtime" == "rust-native" &&
    "$to_stage" != "find-object" &&
    "$to_stage" != "separate-block" &&
    "$to_stage" != "generate-object" ]]; then
    echo "rust-native supports --to-stage find-object, separate-block, or generate-object" >&2
    exit 2
  fi
  if [[ "$runtime" == "rust-native" ]]; then
    bash scripts/runtime/build-pipeline-core-native.sh >/dev/null
  else
    bash scripts/runtime/build-pipeline-core.sh >/dev/null
  fi
  runtime_lang_path_managed=0
  if [[ -n "$tessdata" ]]; then
    runtime_lang_path="$tessdata"
  elif [[ -n "${ITTM_OCR_LANG_PATH:-}" ]]; then
    runtime_lang_path="$ITTM_OCR_LANG_PATH"
  elif [[ -n "${BROWSER_OCR_LANG_PATH:-}" ]]; then
    runtime_lang_path="$BROWSER_OCR_LANG_PATH"
  else
    runtime_lang_path="$PWD/.cache/tessdata"
    runtime_lang_path_managed=1
  fi
  if [[ ! -d "$runtime_lang_path" ]] ||
    ! find "$runtime_lang_path" -maxdepth 1 -type f -name '*.traineddata' \
      -print -quit 2>/dev/null | grep -q .; then
    if [[ "$runtime_lang_path_managed" -eq 1 ]]; then
      BROWSER_OCR_LANG_PATH="$runtime_lang_path" \
        bash scripts/models/download-browser-tessdata.sh >/dev/null
    else
      echo "Explicit tessdata has no traineddata models: $runtime_lang_path" >&2
      exit 2
    fi
  fi
  printf 'RUN %s commit=%s runtime=%s items=%s\n' \
    "$run_dir" "$(git rev-parse --short HEAD)" "$runtime" "${#ITEM_IDS[@]}"
  for item_id in "${ITEM_IDS[@]}"; do
    item_dir="$run_dir/items/$item_id"
    mkdir -p "$item_dir/logs"
    target_status="$item_dir/${STAGE_DIR[$to_stage]}/.runner-status"
    if [[ "$resume_from_last_good" -eq 1 && "$replace_stage" -eq 0 &&
      -f "$target_status" &&
      "$(cat "$target_status")" == "COMPLETE" ]]; then
      printf '  %s REUSED through %s\n' "$item_id" "$to_stage"
      continue
    fi
    if [[ "$runtime" == "rust-native" ]]; then
      command=(
        env
        "ITTM_PIPELINE_CORE_LIB=$PWD/pipeline-core/target/release/libittm_pipeline_core.so"
        "TESSDATA_PREFIX=$runtime_lang_path"
        python3 scripts/debug/debug-rust-separated.py
        "${ITEM_SOURCE[$item_id]}" --output "$item_dir" --engine tesseract
        --tessdata "$runtime_lang_path"
        --to-stage "$to_stage"
      )
    else
      browser_lang_path="$runtime_lang_path"
      if [[ -z "$browser_lang_path" ]]; then
        common_git_dir="$(realpath "$(git rev-parse --git-common-dir)")"
        workspace_parent="$(dirname "$(dirname "$common_git_dir")")"
        while IFS= read -r candidate; do
          if find "$candidate" -maxdepth 1 -type f -name '*.traineddata' \
            -print -quit 2>/dev/null | grep -q .; then
            browser_lang_path="$candidate"
            break
          fi
        done < <(
          printf '%s\n' "$PWD/.cache/tessdata" /usr/share/tessdata
          find "$workspace_parent" -maxdepth 5 -type f \
            -path '*/.cache/tessdata/rus.traineddata' -printf '%h\n' \
            2>/dev/null | sort -u
        )
      fi
      if [[ -z "$browser_lang_path" ]]; then
        echo "Could not resolve browser tessdata with at least one traineddata model" >"$item_dir/logs/runtime.log"
        overall_failed=1
        printf '  %s FAILED; browser tessdata is unavailable\n' "$item_id"
        continue
      fi
      command=(
        env BROWSER_OCR_LANG_PATH="$browser_lang_path"
        node --import tsx scripts/benchmark/benchmark-browser-ocr.ts
        --artifacts "$item_dir" "${ITEM_SOURCE[$item_id]}"
      )
    fi
    if "${command[@]}" >"$item_dir/logs/runtime.log" 2>&1; then
      for ((index=0; index<=to_index; index++)); do
        stage="${STAGES[$index]}"
        stage_status="COMPLETE"
        if [[ "$stage" == "geometry" || "$stage" == "topology" ]]; then
          stage_status="OPAQUE"
        fi
        printf '%s\n' "$stage_status" >"$item_dir/${STAGE_DIR[$stage]}/.runner-status"
        printf '%s\t%s\t%s\t%s\t%s\tfalse\t0\n' \
          "$(date --iso-8601=seconds)" "$item_id" "$stage" \
          "$stage_status" "${ITEM_SOURCE[$item_id]}" >>"$status_file"
      done
      printf '  %s COMPLETE\n' "$item_id"
    else
      overall_failed=1
      printf '  %s FAILED; see %s\n' "$item_id" "$item_dir/logs/runtime.log"
    fi
  done
  if [[ "$overall_failed" -eq 0 ]]; then
    printf 'COMPLETE\n' >"$run_dir/run.status"
    exit 0
  fi
  printf 'FAILED\n' >"$run_dir/run.status"
  exit 3
fi

printf 'RUN %s commit=%s stages=%s..%s items=%s\n' \
  "$run_dir" "$(git rev-parse --short HEAD)" "$from_stage" "$to_stage" "${#ITEM_IDS[@]}"

for item_id in "${ITEM_IDS[@]}"; do
  item_dir="$run_dir/items/$item_id"
  mkdir -p "$item_dir/logs"
  item_blocked=0
  printf '\nITEM %s source=%s\n' "$item_id" "${ITEM_SOURCE[$item_id]}"

  for ((index=from_index; index<=to_index; index++)); do
    stage="${STAGES[$index]}"
    stage_dir="$item_dir/${STAGE_DIR[$stage]}"
    log_file="$item_dir/logs/${STAGE_DIR[$stage]}.log"
    injected="false"
    if override="$(resolve_override "$stage" "$item_id" 2>/dev/null)"; then
      input="$override"
      injected="true"
    else
      input="$(default_input "$stage" "$item_id")"
    fi
    input="$(normalize_stage_input "$stage" "$input")"

    if [[ "$item_blocked" -eq 1 && "$continue_on_failure" -eq 0 ]]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(date --iso-8601=seconds)" "$item_id" "$stage" "SKIPPED" \
        "$input" "$injected" "-" >> "$status_file"
      printf '  %-18s SKIPPED after first BAD\n' "$stage"
      continue
    fi

    if [[ -e "$stage_dir" ]]; then
      if [[ "$resume_from_last_good" -eq 1 && "$replace_stage" -eq 0 ]] &&
        [[ -f "$stage_dir/.runner-status" ]] &&
        [[ "$(<"$stage_dir/.runner-status")" == "COMPLETE" ]]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$(date --iso-8601=seconds)" "$item_id" "$stage" "REUSED" \
          "$input" "$injected" "0" >> "$status_file"
        printf '  %-18s REUSED\n' "$stage"
        continue
      elif [[ "$replace_stage" -eq 1 || "$resume_from_last_good" -eq 1 ]]; then
        find "$stage_dir" -type f -delete
        find "$stage_dir" -depth -type d -empty -delete
      elif [[ -f "$stage_dir/.runner-status" ]] &&
        [[ "$(<"$stage_dir/.runner-status")" == "COMPLETE" ]]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$(date --iso-8601=seconds)" "$item_id" "$stage" "REUSED" \
          "$input" "$injected" "0" >> "$status_file"
        printf '  %-18s REUSED\n' "$stage"
        continue
      else
        echo "Stage directory exists without manifest: $stage_dir" >&2
        overall_failed=1
        item_blocked=1
        continue
      fi
    fi

    printf '  %-18s START input=%s%s\n' \
      "$stage" "$input" "$([[ "$injected" == "true" ]] && printf ' [INJECTED]')"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date --iso-8601=seconds)" "$item_id" "$stage" "STARTED" \
      "$input" "$injected" "-" >> "$status_file"

    exit_code=0
    case "$stage" in
      preprocess)
        work="$item_dir/.work-preprocess-$RANDOM"
        selected_preprocess_step="$(preprocess_step_for "${ITEM_SOURCE[$item_id]}")"
        if run_preprocess "$input" "$stage_dir" "$work" \
          "$selected_preprocess_step" >"$log_file" 2>&1; then
          exit_code=0
        else
          exit_code=$?
        fi
        ;;
      geometry)
        work="$item_dir/.work-geometry-$RANDOM"
        if run_geometry "$input" "$stage_dir" "$work" >"$log_file" 2>&1; then
          exit_code=0
        else
          exit_code=$?
        fi
        ;;
      topology)
        if run_topology "$input" "$stage_dir" >"$log_file" 2>&1; then
          exit_code=0
        else
          exit_code=$?
        fi
        ;;
      find-object)
        if run_find_object "$input" "$stage_dir" >"$log_file" 2>&1; then
          exit_code=0
        else
          exit_code=$?
        fi
        ;;
      separate-block|ocr-blocks|get-segment|generate-object)
        if run_object_stage \
          "$stage" "$input" "$stage_dir" "${ITEM_REFERENCE[$item_id]}" \
          >"$log_file" 2>&1; then
          exit_code=0
        else
          exit_code=$?
        fi
        ;;
    esac

    mkdir -p "$stage_dir"
    write_input_manifest \
      "$stage_dir/input.json" "$item_id" "$stage" "$input" "$injected"
    if [[ "$exit_code" -eq 0 ]]; then
      status="COMPLETE"
    else
      status="FAILED"
      overall_failed=1
      item_blocked=1
    fi
    printf '%s\n' "$status" > "$stage_dir/.runner-status"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date --iso-8601=seconds)" "$item_id" "$stage" "$status" \
      "$input" "$injected" "$exit_code" >> "$status_file"
    printf '  %-18s %s\n' "$stage" "$status"
  done
done

if [[ "$overall_failed" -eq 0 ]]; then
  final_status="COMPLETE"
elif [[ "$diagnostic_batch" -eq 1 ]]; then
  final_status="COMPLETE_WITH_FAILURES"
else
  final_status="FAILED"
fi
printf '%s\n' "$final_status" > "$run_dir/run.status"
message="debug-all-separated $final_status: $run_dir"
printf '%s\n' "$message" > "$run_dir/notification.txt"
printf '\n%s\n' "$message"
if [[ "$notify_terminal" -eq 1 ]]; then
  notify_tty="$(tty 2>/dev/null || true)"
  if [[ "$notify_tty" == /dev/* && -w "$notify_tty" ]]; then
    printf '\a\033]9;%s\007\n' "$message" >"$notify_tty" 2>/dev/null || true
  fi
fi

if [[ "$overall_failed" -ne 0 && "$diagnostic_batch" -ne 1 ]]; then
  exit 3
fi
