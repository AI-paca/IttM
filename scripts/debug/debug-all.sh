#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/debug/debug-all.sh [options]

Defaults:
  - fixtures: debug/fixtures/, then debug/ or testtables/ fallback
  - engines: tesseract,easyocr,browser-tesseract
  - backend flags: automatic per engine
  - output: debug/result.csv and debug/time.csv
  - tmp: debug/tmp/<engine>

Options:
  --fixture GLOB                 Run one fixture or glob; may be repeated.
  --engines CSV                  Engines to run, e.g. tesseract or tesseract,easyocr.
  --pipeline-profile PROFILE     Use one backend profile for every backend engine.
  --engine-profile ENGINE=PROFILE
                                 Override backend profile for one engine.
  --browser-profile PROFILE      Browser profile; default browser_tesseract_dewarp.
  --gpu auto|on|off              Backend Docker GPU mode; default auto.
  --pages PAGES                  Backend PDF pages, e.g. 1,3,5-7.
  --fixture-pages GLOB=PAGES     Backend PDF pages for one fixture glob.
  --max-pages N                  Backend first N PDF pages.
  --fixture-max-pages GLOB=N     Backend first N PDF pages for one fixture glob.
  --no-pdf-raster                Do not add PDF raster PNG/JPEG rows.
  --pdf-raster-only              Run only generated PDF raster rows.
  --pdf-raster-formats CSV       Raster formats for selected PDFs; default png,jpg.
  --pdf-raster-max-pages N       First N PDF pages for raster rows; default 5.
  --pdf-raster-dpi N             PDF raster DPI; default 300.
  --timeout SECONDS              Per-file timeout; default 900.
  --resume                       Reuse existing tmp markdown outputs.
  --source DIR                   Worktree source; default current directory.
  --fixtures DIR                 Fixture directory; default debug/fixtures.
  --expected-root DIR            Manual reference directory; default debug/reference.
  --tmp-root DIR                 Intermediate output root; default debug/tmp.
  --output-root DIR              Final CSV directory; default debug.
  --require-complete-scoring     Fail on missing_reference/not_checked corpus rows.

API engines are scaffolded as tmp folders but are not implemented yet:
api-ollama, api-openrouter, api-gemini.
EOF
}

source_root="."
fixtures_root=""
expected_root="debug/reference"
default_fixtures_root="debug/fixtures"
tmp_root="debug/tmp"
output_root="debug"
engines_csv="${OCR_DEBUG_ENGINES:-tesseract,easyocr,browser-tesseract}"
browser_profile="${BROWSER_OCR_PROFILE:-browser_tesseract_dewarp}"
gpu_mode="${OCR_BENCHMARK_GPU:-auto}"
timeout_seconds=900
fixture_patterns=()
backend_profile_args=()
backend_page_args=(--fixture-max-pages 'Adobe Scan Oct 26, 2022 (1).pdf=5')
resume_arg=()
pdf_raster=1
pdf_raster_only=0
pdf_raster_formats="png,jpg"
pdf_raster_max_pages=5
pdf_raster_dpi=300
require_complete_scoring=0

if [[ -f debug/.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source debug/.env
  set +a
fi

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
      fixture_patterns+=("$2")
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
    --browser-profile)
      browser_profile="$2"
      shift 2
      ;;
    --gpu)
      gpu_mode="$2"
      shift 2
      ;;
    --pages)
      backend_page_args+=(--pages "$2")
      shift 2
      ;;
    --fixture-pages)
      backend_page_args+=(--fixture-pages "$2")
      shift 2
      ;;
    --max-pages)
      backend_page_args+=(--max-pages "$2")
      shift 2
      ;;
    --fixture-max-pages)
      backend_page_args+=(--fixture-max-pages "$2")
      shift 2
      ;;
    --no-pdf-raster)
      pdf_raster=0
      shift
      ;;
    --pdf-raster-only)
      pdf_raster=1
      pdf_raster_only=1
      shift
      ;;
    --pdf-raster-formats)
      pdf_raster_formats="$2"
      shift 2
      ;;
    --pdf-raster-max-pages)
      pdf_raster_max_pages="$2"
      shift 2
      ;;
    --pdf-raster-dpi)
      pdf_raster_dpi="$2"
      shift 2
      ;;
    --timeout)
      timeout_seconds="$2"
      shift 2
      ;;
    --require-complete-scoring)
      require_complete_scoring=1
      shift
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

mkdir -p \
  "$tmp_root" \
  "$tmp_root/tesseract" \
  "$tmp_root/easyocr" \
  "$tmp_root/browser-tesseract" \
  "$tmp_root/api-ollama" \
  "$tmp_root/api-openrouter" \
  "$tmp_root/api-gemini" \
  "$output_root"

if [[ ! "$pdf_raster_max_pages" =~ ^[1-9][0-9]*$ ]]; then
  echo "--pdf-raster-max-pages must be a positive integer" >&2
  exit 2
fi
if [[ ! "$pdf_raster_dpi" =~ ^[1-9][0-9]*$ ]]; then
  echo "--pdf-raster-dpi must be a positive integer" >&2
  exit 2
fi

has_supported_fixtures() {
  local root="$1"
  [[ -d "$root" ]] || return 1
  find "$root" -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.png' -o -iname '*.jpg' \
       -o -iname '*.jpeg' -o -iname '*.webp' \) \
    -print -quit | grep -q .
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

IFS=',' read -r -a requested_engines <<< "$engines_csv"
backend_engines=()
run_browser=0
for engine in "${requested_engines[@]}"; do
  case "$engine" in
    tesseract|easyocr|auto)
      backend_engines+=("$engine")
      ;;
    browser-tesseract)
      run_browser=1
      ;;
    api-ollama|api-openrouter|api-gemini)
      echo "Debug API engine '$engine' is scaffolded but not implemented yet." >&2
      echo "Configure future API keys in debug/.env (see debug/.env.sample)." >&2
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

matches_fixture_patterns() {
  local file_name="$1"
  local pattern
  if [[ ${#fixture_patterns[@]} -eq 0 ]]; then
    return 0
  fi
  for pattern in "${fixture_patterns[@]}"; do
    if [[ "$file_name" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

stage_debug_file() {
  local source="$1"
  local target="$2"
  if ! ln "$(realpath "$source")" "$target" 2>/dev/null; then
    cp "$(realpath "$source")" "$target"
  fi
}

aggregate_raster_reference_name() {
  local file_name="$1"
  if [[ "$file_name" =~ ^(.+\.pdf)\.page-[0-9]{3}\.raster\.(png|jpg)$ ]]; then
    printf '%s.raster.%s.md\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  fi
}

mapfile -t selected_fixtures < <(
  find "$fixtures_root" -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.png' -o -iname '*.jpg' \
       -o -iname '*.jpeg' -o -iname '*.webp' \) \
    -printf '%p\n' | sort
)
if [[ ${#fixture_patterns[@]} -gt 0 ]]; then
  filtered_fixtures=()
  for fixture in "${selected_fixtures[@]}"; do
    if matches_fixture_patterns "$(basename "$fixture")"; then
      filtered_fixtures+=("$fixture")
    fi
  done
  selected_fixtures=("${filtered_fixtures[@]}")
fi

raster_outputs=()
if [[ "$pdf_raster" -eq 1 && ${#selected_fixtures[@]} -gt 0 ]]; then
  selected_pdf_fixtures=()
  for fixture in "${selected_fixtures[@]}"; do
    file_name="$(basename "$fixture")"
    if [[ "${file_name,,}" != *.pdf ]]; then
      continue
    fi
    representative_reference=0
    IFS=',' read -r -a requested_raster_formats <<< "$pdf_raster_formats"
    for raster_format in "${requested_raster_formats[@]}"; do
      raster_format="${raster_format,,}"
      [[ "$raster_format" == "jpeg" ]] && raster_format="jpg"
      if [[ -f "$expected_root/$file_name.raster.$raster_format.md" ]]; then
        representative_reference=1
        break
      fi
    done
    if [[ -f "$expected_root/$file_name.md" || "$representative_reference" -eq 1 ]]; then
      selected_pdf_fixtures+=("$fixture")
    else
      echo "Skipping PDF raster for $file_name: no PDF or raster reference" >&2
    fi
  done

  if [[ ${#selected_pdf_fixtures[@]} -gt 0 ]]; then
    rm -rf "$tmp_root/pdf-image-fixtures" "$tmp_root/pdf-image-reference"
    mapfile -t raster_outputs < <(
      python3 scripts/debug/debug_pdf_image_probe.py \
        "${selected_pdf_fixtures[@]}" \
        --expected-root "$expected_root" \
        --output-dir "$tmp_root/pdf-image-fixtures" \
        --probe-reference-root "$tmp_root/pdf-image-reference" \
        --max-pages "$pdf_raster_max_pages" \
        --dpi "$pdf_raster_dpi" \
        --format "$pdf_raster_formats"
    )
  fi
fi

# Keep every generated raster in the corpus. Exact page references are staged
# per page. A document-level raster reference is scored once against an ordered
# aggregate candidate, never duplicated onto each generated page.
raster_skip_ledger="$tmp_root/unscored-raster-fixtures.txt"
: > "$raster_skip_ledger"
if [[ ${#raster_outputs[@]} -gt 0 ]]; then
  for fixture in "${raster_outputs[@]}"; do
    file_name="$(basename "$fixture")"
    raster_reference="$tmp_root/pdf-image-reference/$file_name.md"
    aggregate_reference_name="$(aggregate_raster_reference_name "$file_name")"
    aggregate_reference=""
    if [[ -n "$aggregate_reference_name" ]]; then
      aggregate_reference="$tmp_root/pdf-image-reference/$aggregate_reference_name"
    fi
    if [[ ! -f "$raster_reference" && ! -f "$aggregate_reference" ]]; then
      printf '%s\t%s\n' \
        "$fixture" \
        "missing_reference: exact or representative raster reference is unavailable" \
        >> "$raster_skip_ledger"
      echo "Keeping unscored PDF raster as missing_reference: $file_name" >&2
    fi
  done
fi

if [[ ${#raster_outputs[@]} -gt 0 ]]; then
  combined_fixtures_root="$tmp_root/fixtures"
  combined_reference_root="$tmp_root/combined-reference"
  rm -rf "$combined_fixtures_root" "$combined_reference_root"
  mkdir -p "$combined_fixtures_root" "$combined_reference_root"

  if [[ "$pdf_raster_only" -eq 0 ]]; then
    for fixture in "${selected_fixtures[@]}"; do
      file_name="$(basename "$fixture")"
      stage_debug_file "$fixture" "$combined_fixtures_root/$file_name"
      if [[ -f "$expected_root/$file_name.md" ]]; then
        stage_debug_file "$expected_root/$file_name.md" "$combined_reference_root/$file_name.md"
      fi
    done
  fi

  for fixture in "${raster_outputs[@]}"; do
    file_name="$(basename "$fixture")"
    stage_debug_file "$fixture" "$combined_fixtures_root/$file_name"
    if [[ -f "$tmp_root/pdf-image-reference/$file_name.md" ]]; then
      stage_debug_file \
        "$tmp_root/pdf-image-reference/$file_name.md" \
        "$combined_reference_root/$file_name.md"
    fi
  done
  while IFS= read -r aggregate_reference; do
    file_name="$(basename "$aggregate_reference")"
    stage_debug_file \
      "$aggregate_reference" \
      "$combined_reference_root/$file_name"
  done < <(
    find "$tmp_root/pdf-image-reference" -maxdepth 1 -type f \
      \( -iname '*.pdf.raster.png.md' -o -iname '*.pdf.raster.jpg.md' \) \
      -printf '%p\n' | sort
  )

  fixtures_root="$combined_fixtures_root"
  expected_root="$combined_reference_root"
  fixture_patterns=()
fi

fixture_args=()
for pattern in "${fixture_patterns[@]}"; do
  fixture_args+=(--fixture "$pattern")
done

# Page selection applies only to native PDF inputs. Phase 2 has already
# materialized bounded page rasters, and image-only phases must keep their
# stable expected-root alive until debug_report.py has scored every row.
has_selected_pdf_fixtures() {
  local fixture
  while IFS= read -r fixture; do
    if matches_fixture_patterns "$(basename "$fixture")"; then
      return 0
    fi
  done < <(
    find "$fixtures_root" -maxdepth 1 -type f -iname '*.pdf' -printf '%p\n'
  )
  return 1
}

if ! has_selected_pdf_fixtures; then
  backend_page_args=()
fi

has_selected_browser_fixtures() {
  local browser_fixtures_root="$fixtures_root"
  [[ -d "$browser_fixtures_root" ]] || return 1

  local fixture file_name pattern
  while IFS= read -r fixture; do
    if [[ ${#fixture_patterns[@]} -eq 0 ]]; then
      return 0
    fi
    file_name="$(basename "$fixture")"
    for pattern in "${fixture_patterns[@]}"; do
      if [[ "$file_name" == $pattern ]]; then
        return 0
      fi
    done
  done < <(
    find "$browser_fixtures_root" -maxdepth 1 -type f \
      \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) \
      -printf '%p\n'
  )
  return 1
}

if [[ ${#backend_engines[@]} -gt 0 ]]; then
  backend_csv="$(IFS=,; echo "${backend_engines[*]}")"
  scripts/debug/run-debug.sh \
    --source "$source_root" \
    --fixtures "$fixtures_root" \
    --expected-root "$expected_root" \
    --output "$tmp_root" \
    --engines "$backend_csv" \
    --gpu "$gpu_mode" \
    --timeout "$timeout_seconds" \
    "${backend_page_args[@]}" \
    "${fixture_args[@]}" \
    "${backend_profile_args[@]}" \
    "${resume_arg[@]}"
fi

browser_root=""
if [[ "$run_browser" -eq 1 ]]; then
  if has_selected_browser_fixtures; then
    browser_root="$tmp_root/browser-tesseract"
    scripts/debug/run-browser-debug.sh \
      --source "$source_root" \
      --fixtures "$fixtures_root" \
      --output "$browser_root" \
      --timeout "$timeout_seconds" \
      --profile "$browser_profile" \
      "${fixture_args[@]}" \
      "${resume_arg[@]}"
  else
    echo "Skipping browser-tesseract: no selected image fixtures."
  fi
fi

matrix_args=(
  --benchmark-root "$tmp_root"
  --expected-root "$expected_root"
  --output-root "$output_root"
)
if [[ -n "$browser_root" ]]; then
  matrix_args+=(--browser-root "$browser_root")
fi
python3 scripts/debug/debug_matrix_report.py "${matrix_args[@]}"
quality_gate_args=(--result "$output_root/result.csv")
if [[ "$require_complete_scoring" -eq 1 ]]; then
  quality_gate_args+=(--require-complete-scoring)
fi
python3 scripts/debug/debug_quality_gate.py "${quality_gate_args[@]}"
