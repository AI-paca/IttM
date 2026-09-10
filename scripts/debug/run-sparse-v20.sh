#!/usr/bin/env bash
set -Eeuo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repository_root"

run_id="${RUN_ID:-v20-$(date +%Y%m%d-%H%M%S)}"
if [[ ! "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "RUN_ID contains unsafe characters: $run_id" >&2
  exit 64
fi

gate_root="${V20_GATE_OUTPUT:-debug/tmp/v20-gates}"
gate_dir="$gate_root/$run_id"
mkdir -p "$gate_root"
if ! mkdir "$gate_dir"; then
  echo "immutable v20 gate directory already exists: $gate_dir" >&2
  exit 73
fi
mkdir "$gate_dir/logs"
gate_tsv="$gate_dir/gates.tsv"
printf 'gate\tstatus\tseconds\tlog\n' > "$gate_tsv"

finished=0
active_gate="bootstrap"
notify() {
  local urgency="$1"
  local title="$2"
  local body="$3"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send --urgency="$urgency" "$title" "$body" >/dev/null 2>&1 || true
  fi
}

on_exit() {
  local rc=$?
  set +e
  if [[ "$finished" -eq 1 && "$rc" -eq 0 ]]; then
    notify normal "IttM sparse v20: GREEN" "Run $run_id completed; all gates are GREEN."
  else
    notify critical "IttM sparse v20: RED" "Run $run_id failed in gate $active_gate (exit $rc)."
  fi
}
trap on_exit EXIT

run_gate() {
  local name="$1"
  shift
  active_gate="$name"
  local log="$gate_dir/logs/$name.log"
  local started=$SECONDS
  local rc
  local tee_rc
  local -a pipe_status
  set +e
  "$@" 2>&1 | tee "$log"
  pipe_status=("${PIPESTATUS[@]}")
  rc=${pipe_status[0]}
  tee_rc=${pipe_status[1]}
  set -e
  if [[ "$rc" -eq 0 && "$tee_rc" -ne 0 ]]; then
    rc=$tee_rc
  fi
  if [[ "$rc" -eq 0 ]]; then
    printf '%s\tGREEN\t%s\t%s\n' "$name" "$((SECONDS - started))" "$log" >> "$gate_tsv"
  else
    printf '%s\tRED\t%s\t%s\n' "$name" "$((SECONDS - started))" "$log" >> "$gate_tsv"
  fi
  return "$rc"
}

python_bin="${V20_PYTHON:-python3}"
pytest_bin="${V20_PYTEST:-pytest}"

run_gate stage3-control "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_recursive_control.py \
  ocr/tests/sparse_pipeline/test_bounded_grammar.py \
  ocr/tests/sparse_pipeline/test_recursive_invariants.py \
  ocr/tests/sparse_pipeline/test_corpus_debug.py

run_gate stage1-geometry "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_geometry_stage.py \
  ocr/tests/sparse_pipeline/test_synthetic_geometry.py \
  ocr/tests/sparse_pipeline/test_geometry_gate.py \
  ocr/tests/sparse_pipeline/test_geometry_rgb_binding.py

run_gate stage6-objects "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_object_reconstruction_stage6.py

run_gate stage4-enhancement "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_crop_enhancement_stage4.py

run_gate stage5-blocks "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_block_planning_stage5.py

run_gate stage2-ocr "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_ocr_adapters_stage2.py \
  ocr/tests/sparse_pipeline/test_ocr_queue_stage2.py \
  ocr/tests/sparse_pipeline/test_ocr_session_stage2.py \
  ocr/tests/sparse_pipeline/test_ocr_rpc_stage2.py \
  ocr/tests/sparse_pipeline/test_ocr_fusion_stage2.py \
  ocr/tests/sparse_pipeline/test_ocr_artifacts_stage2.py \
  ocr/tests/sparse_pipeline/test_debug_ocr_blocks_stage2.py

run_gate stage7-document "$pytest_bin" -q \
  ocr/tests/sparse_pipeline/test_document_assembly_stage7.py \
  ocr/tests/sparse_pipeline/test_document_assembly_stage7_adversarial_audit.py \
  ocr/tests/sparse_pipeline/test_document_structure_stage7.py \
  ocr/tests/sparse_pipeline/test_document_artifacts_stage7.py \
  ocr/tests/sparse_pipeline/test_debug_document_assembly_stage7.py \
  ocr/tests/sparse_pipeline/test_validate_v20_summary.py \
  ocr/tests/sparse_pipeline/test_tutorial_artifacts.py \
  ocr/tests/sparse_pipeline/test_debug_sparse_tutorial.py

input="${V20_INPUT:-}"
reference_args=()
problem_inputs=(
  "debug/fixtures/000041301_UchebPlan_sign000029629.pdf.raster.png"
  "debug/fixtures/09.03.03_05(ИУ1).pdf.raster.png"
  "debug/fixtures/Adobe Scan Jun 20, 2026.pdf.raster.png"
)
default_evidence_only_source="Adobe Scan Jun 20, 2026.pdf.raster.png"
evidence_only_value="${V20_EVIDENCE_ONLY_SOURCES-$default_evidence_only_source}"
evidence_only_sources=()
if [[ -n "$evidence_only_value" ]]; then
  IFS=':' read -r -a evidence_only_sources <<< "$evidence_only_value"
fi
declare -A seen_evidence_sources=()
for evidence_source in "${evidence_only_sources[@]}"; do
  if [[ -z "$evidence_source" ]]; then
    echo "V20_EVIDENCE_ONLY_SOURCES contains an empty selector" >&2
    exit 64
  fi
  if [[ -n "${seen_evidence_sources[$evidence_source]+present}" ]]; then
    echo "V20_EVIDENCE_ONLY_SOURCES contains a duplicate: $evidence_source" >&2
    exit 64
  fi
  seen_evidence_sources["$evidence_source"]=1
done
required_problem_sources=()
if [[ -z "$input" ]]; then
  missing_problem_inputs=()
  for candidate in "${problem_inputs[@]}"; do
    if [[ ! -f "$candidate" ]]; then
      missing_problem_inputs+=("$candidate")
    fi
  done
  if (( ${#missing_problem_inputs[@]} )); then
    printf 'required v20 failure-corpus input is missing: %s\n' \
      "${missing_problem_inputs[@]}" >&2
    echo "run scripts/debug/debug-all.sh first to materialize the raster corpus" >&2
    exit 66
  fi
  input="debug/fixtures"
  reference_args=(--reference-root "${V20_REFERENCE_ROOT:-debug/reference}")
  required_problem_sources=(
    "000041301_UchebPlan_sign000029629.pdf.raster.png"
    "09.03.03_05(ИУ1).pdf.raster.png"
    "Adobe Scan Jun 20, 2026.pdf.raster.png"
  )
elif [[ -n "${V20_REFERENCE_ROOT:-}" ]]; then
  reference_args=(--reference-root "$V20_REFERENCE_ROOT")
fi

# The corpus replay is bounded explicitly, but its default acceptance set must
# contain the three real regressions above; SAMPLE alone is never sufficient.
v20_limit="${V20_LIMIT:-24}"
if [[ ! "$v20_limit" =~ ^[1-9][0-9]*$ ]]; then
  echo "V20_LIMIT must be a positive integer" >&2
  exit 64
fi

document_output="${V20_DOCUMENT_OUTPUT:-debug/tmp/document-corpus}"
document_run_id="$run_id-corpus"
v20_minimum_accuracy="91.0"
runner_args=(
  scripts/debug/debug_document_assembly.py
  --input "$input"
  --output "$document_output"
  --run-id "$document_run_id"
  --limit "$v20_limit"
  --engines "${V20_ENGINES:-tesseract}"
  --easy-python "${V20_EASY_PYTHON:-/home/alpaca/GitHub/IttM-engine-original/ocr/.venv/bin/python}"
  --easy-models "${V20_EASY_MODELS:-/home/alpaca/.EasyOCR/model}"
  --easy-device "${V20_EASY_DEVICE:-cuda}"
  --prepare-workers "${V20_PREPARE_WORKERS:-4}"
  --prepare-window "${V20_PREPARE_WINDOW:-8}"
  --ocr-page-workers "${V20_OCR_PAGE_WORKERS:-2}"
  --ocr-window "${V20_OCR_WINDOW:-4}"
  --tesseract-psm "${V20_SINGLE_CONTEXT_PSM:-6}"
  --tesseract-workers "${V20_TESSERACT_WORKERS:-4}"
  --tesseract-upscale-min-height "${V20_TESSERACT_UPSCALE_MIN_HEIGHT:-320}"
  --tesseract-upscale-max-factor "${V20_TESSERACT_UPSCALE_MAX_FACTOR:-4}"
  --tesseract-upscale-max-pixels "${V20_TESSERACT_UPSCALE_MAX_PIXELS:-16000000}"
  --fail-on-unresolved
  --minimum-accuracy-percent "$v20_minimum_accuracy"
  "${reference_args[@]}"
)
for evidence_source in "${evidence_only_sources[@]}"; do
  [[ -n "$evidence_source" ]] || continue
  evidence_match=0
  if [[ -f "$input" && "$(basename "$input")" == "$evidence_source" ]]; then
    evidence_match=1
  elif [[ -d "$input" ]]; then
    if [[ -f "$input/$evidence_source" ]]; then
      evidence_match=1
    elif find "$input" -type f -name "$evidence_source" -print -quit | grep -q .; then
      evidence_match=1
    fi
  fi
  if (( evidence_match )); then
    runner_args+=(--evidence-only-source "$evidence_source")
  fi
done
known_text_rc=0
run_gate stage7-known-text "$python_bin" "${runner_args[@]}" || known_text_rc=$?

document_summary="$document_output/$document_run_id/summary.json"
summary_exists_rc=0
run_gate stage7-summary-exists test -f "$document_summary" || summary_exists_rc=$?
summary_validator_args=(
  scripts/debug/validate_v20_summary.py
  --summary "$document_summary"
)
for required_source in "${required_problem_sources[@]}"; do
  summary_validator_args+=(--required-source "$required_source")
done
for evidence_source in "${evidence_only_sources[@]}"; do
  [[ -n "$evidence_source" ]] || continue
  summary_validator_args+=(--evidence-only-source "$evidence_source")
done
summary_green_rc=0
run_gate stage7-summary-green \
  "$python_bin" "${summary_validator_args[@]}" || summary_green_rc=$?

tutorial_set="${V20_TUTORIAL_SET:-problem}"
tutorial_inputs=()
case "$tutorial_set" in
  problem)
    tutorial_inputs=("${problem_inputs[@]}")
    ;;
  all)
    if [[ -f "$input" ]]; then
      tutorial_inputs=("$input")
    else
      mapfile -d '' tutorial_inputs < <(
        find "$input" -type f -name '*.png' \
          ! -name '*.mask.png' \
          ! -name '*-overlay.png' \
          ! -name '*source.png' \
          ! -name '*aligned.png' \
          -print0 | sort -z
      )
    fi
    ;;
  input)
    if [[ -z "${V20_TUTORIAL_INPUTS:-}" ]]; then
      echo "V20_TUTORIAL_INPUTS is required for V20_TUTORIAL_SET=input" >&2
      exit 64
    fi
    IFS=':' read -r -a tutorial_inputs <<< "${V20_TUTORIAL_INPUTS}"
    ;;
  sample)
    tutorial_inputs=("debug/fixtures/SAMPLE_4k.png")
    ;;
  *)
    echo "V20_TUTORIAL_SET must be problem, all, input, or sample" >&2
    exit 64
    ;;
esac
if (( ${#tutorial_inputs[@]} == 0 )); then
  echo "v20 tutorial input set is empty: $tutorial_set" >&2
  exit 66
fi
tutorial_args=(
  scripts/debug/debug_sparse_tutorial.py
  --reference-root "${V20_REFERENCE_ROOT:-debug/reference}"
  --output "${V20_ARTIFACT_OUTPUT:-debug/artifacts/v20}"
  --run-id "$run_id"
  --report "${V20_ROOT_REPORT:-debag-v20-report.md}"
  --minimum-accuracy-percent "$v20_minimum_accuracy"
  --enhancement-backend "${V20_ENHANCEMENT_BACKEND:-numpy}"
  --engines "${V20_ENGINES:-tesseract}"
  --easy-python "${V20_EASY_PYTHON:-/home/alpaca/GitHub/IttM-engine-original/ocr/.venv/bin/python}"
  --easy-models "${V20_EASY_MODELS:-/home/alpaca/.EasyOCR/model}"
  --easy-device "${V20_EASY_DEVICE:-cuda}"
  --block-mode spatial_2d
  --block-padding "${V20_BLOCK_PADDING:-24}"
  --block-max-core-segments "${V20_BLOCK_MAX_CORE_SEGMENTS:-24}"
  --single-context-psm "${V20_SINGLE_CONTEXT_PSM:-6}"
  --document-context-psm "${V20_DOCUMENT_CONTEXT_PSM:-4}"
  --tesseract-workers "${V20_TESSERACT_WORKERS:-4}"
  --tesseract-upscale-min-height "${V20_TESSERACT_UPSCALE_MIN_HEIGHT:-320}"
  --tesseract-upscale-max-factor "${V20_TESSERACT_UPSCALE_MAX_FACTOR:-4}"
  --tesseract-upscale-max-pixels "${V20_TESSERACT_UPSCALE_MAX_PIXELS:-16000000}"
)
for tutorial_input in "${tutorial_inputs[@]}"; do
  if [[ ! -f "$tutorial_input" ]]; then
    echo "v20 tutorial input is missing: $tutorial_input" >&2
    exit 66
  fi
  tutorial_args+=(--input "$tutorial_input")
done
for evidence_source in "${evidence_only_sources[@]}"; do
  [[ -n "$evidence_source" ]] || continue
  for tutorial_input in "${tutorial_inputs[@]}"; do
    tutorial_basename="$(basename "$tutorial_input")"
    if [[ "$evidence_source" == "$tutorial_basename" || \
          "$evidence_source" == "$tutorial_input" ]]; then
      # Append a configured selector once even when several nested inputs have
      # the same basename; the tutorial applies that selector to every match.
      tutorial_args+=(--evidence-only-source "$evidence_source")
      break
    fi
  done
done
tutorial_rc=0
run_gate stage7-object-artifacts "$python_bin" "${tutorial_args[@]}" || tutorial_rc=$?

tutorial_dir="${V20_ARTIFACT_OUTPUT:-debug/artifacts/v20}/$run_id"
artifact_layout_rc=0
run_gate stage7-artifact-layout "$python_bin" -c '
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
if summary.get("execution_order") != [3, 1, 6, 4, 5, 2, 7]:
    raise SystemExit("tutorial execution order is not exact")
required_root = ("document.md", "document.txt", "provenance.json", "debag-report.md")
missing = [name for name in required_root if not (root / name).is_file()]
if missing:
    raise SystemExit("missing tutorial root artifacts: " + ", ".join(missing))
for item in summary.get("items", []):
    artifact = pathlib.Path(item["artifact"])
    if not artifact.is_absolute():
        artifact = pathlib.Path.cwd() / artifact
    required = (
        "sparse-matrix.json",
        "sparse-matrix.tsv",
        "sparse-matrix.png",
        "sparse-matrix-ownership.png",
        "objects/manifest.json",
        "objects/index.md",
        "logs/stages.tsv",
        "document.md",
        "document.txt",
        "provenance.json",
    )
    absent = [name for name in required if not (artifact / name).is_file()]
    if absent:
        raise SystemExit(f"{artifact}: missing " + ", ".join(absent))
    objects = json.loads((artifact / "objects/manifest.json").read_text(encoding="utf-8"))
    for record in objects.get("objects", []):
        object_root = artifact / "objects" / record["object_id"]
        for name in ("object.md", "object.txt", "object.json", "debag.md", "segments", "blocks"):
            if not (object_root / name).exists():
                raise SystemExit(f"{object_root}: missing {name}")
' "$tutorial_dir" || artifact_layout_rc=$?

if (( known_text_rc || summary_exists_rc || summary_green_rc || tutorial_rc || artifact_layout_rc )); then
  active_gate="stage7-acceptance"
  echo "v20 RED: Stage 7 artifacts were preserved for diagnosis" >&2
  echo "gate table: $gate_tsv" >&2
  echo "document summary: $document_summary" >&2
  echo "full v20 artifacts: $tutorial_dir" >&2
  echo "root report: ${V20_ROOT_REPORT:-debag-v20-report.md}" >&2
  exit 1
fi

active_gate="complete"
finished=1
printf 'run\tGREEN\t%s\t%s\n' "$run_id" "$SECONDS" "$document_summary" >> "$gate_tsv"
echo "v20 GREEN: $run_id"
echo "gate table: $gate_tsv"
echo "document summary: $document_summary"
echo "full v20 artifacts: $tutorial_dir"
echo "root report: ${V20_ROOT_REPORT:-debag-v20-report.md}"
