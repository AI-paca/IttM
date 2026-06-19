#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/benchmark-testtables.sh \
    --source /path/to/commit-worktree \
    --fixtures /path/to/debug-inputs \
    --output /path/to/debug/tmp \
    [--engines tesseract,easyocr] \
    [--pipeline-profile PROFILE] \
    [--engine-profile tesseract=PROFILE] \
    [--gpu auto|on|off] \
    [--expected-root /path/to/manual-expected] \
    [--fixture 'photo*.jpg'] \
    [--pages '1,3,5-7'] \
    [--fixture-pages 'Adobe*.pdf=1-5'] \
    [--max-pages N] \
    [--fixture-max-pages 'Adobe*.pdf=5'] \
    [--timeout 300] \
    [--resume]

The script starts the selected worktree's OCR service in Docker, calls the
direct /convert endpoint with curl, and writes one Markdown result per file and
engine under the output directory. --fixture may be repeated and accepts shell
glob patterns matched against basenames. It stays quiet while OCR is running.
EOF
}

source_root=""
fixtures_root=""
expected_root=""
output_root=""
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
original_args=("$@")
engines_csv="${OCR_BENCHMARK_ENGINES:-tesseract,easyocr}"
pipeline_profile=""
engine_profile_rules=()
gpu_mode="${OCR_BENCHMARK_GPU:-auto}"
page_selection=""
max_pages=""
fixture_page_selection_rules=()
fixture_page_limit_rules=()
timeout_seconds=300
fixture_patterns=()
resume=0
runtime_image="${OCR_BENCHMARK_IMAGE:-ittm-ocr}"
python_packages_volume="${OCR_PYTHON_PACKAGES_VOLUME:-ittm_ocr-python-packages}"
models_volume="${OCR_EASYOCR_MODELS_VOLUME:-ittm_ocr-easyocr-models}"

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
    --engines)
      engines_csv="$2"
      shift 2
      ;;
    --pipeline-profile)
      pipeline_profile="$2"
      shift 2
      ;;
    --engine-profile)
      engine_profile_rules+=("$2")
      shift 2
      ;;
    --gpu)
      gpu_mode="$2"
      shift 2
      ;;
    --fixture)
      fixture_patterns+=("$2")
      shift 2
      ;;
    --pages)
      page_selection="$2"
      shift 2
      ;;
    --fixture-pages)
      fixture_page_selection_rules+=("$2")
      shift 2
      ;;
    --max-pages)
      max_pages="$2"
      shift 2
      ;;
    --fixture-max-pages)
      fixture_page_limit_rules+=("$2")
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

if [[ "$gpu_mode" != "auto" && "$gpu_mode" != "on" && "$gpu_mode" != "off" ]]; then
  echo "--gpu must be one of: auto, on, off" >&2
  exit 2
fi

for required in source_root fixtures_root output_root; do
  if [[ -z "${!required}" ]]; then
    echo "Missing --${required//_root/}" >&2
    usage >&2
    exit 2
  fi
done

source_root="$(realpath "$source_root")"
fixtures_root="$(realpath "$fixtures_root")"
expected_root="${expected_root:-$fixtures_root/expected}"
mkdir -p "$expected_root"
expected_root="$(realpath "$expected_root")"
mkdir -p "$output_root"
output_root="$(realpath "$output_root")"

if [[ ! -d "$source_root/ocr/app" ]]; then
  echo "OCR source not found under $source_root" >&2
  exit 2
fi

mapfile -t fixtures < <(
  find "$fixtures_root" -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.png' -o -iname '*.jpg' \
       -o -iname '*.jpeg' -o -iname '*.webp' \) \
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
  echo "No supported fixtures found in $fixtures_root" >&2
  exit 2
fi

is_page_selection() {
  [[ "$1" =~ ^[1-9][0-9]*(-[1-9][0-9]*)?(,[1-9][0-9]*(-[1-9][0-9]*)?)*$ ]]
}

validate_page_selection_for_pdf() {
  local fixture="$1"
  local pages="$2"
  local page_count token first last

  page_count="$(qpdf --show-npages "$fixture")"
  IFS=',' read -r -a tokens <<< "$pages"
  for token in "${tokens[@]}"; do
    if [[ "$token" == *-* ]]; then
      first="${token%-*}"
      last="${token#*-}"
    else
      first="$token"
      last="$token"
    fi
    if (( first > last )); then
      echo "Invalid page range '$token' for $(basename "$fixture"): start is greater than end" >&2
      return 2
    fi
    if (( last > page_count )); then
      echo "Invalid page range '$token' for $(basename "$fixture"): PDF has $page_count pages" >&2
      return 2
    fi
  done
}

if [[ -n "$page_selection" && -n "$max_pages" ]]; then
  echo "--pages and --max-pages are mutually exclusive" >&2
  exit 2
fi
if [[ -n "$max_pages" && ! "$max_pages" =~ ^[1-9][0-9]*$ ]]; then
  echo "--max-pages must be a positive integer" >&2
  exit 2
fi
if [[ -n "$max_pages" ]]; then
  page_selection="1-$max_pages"
fi
if [[ -n "$page_selection" ]] && ! is_page_selection "$page_selection"; then
  echo "--pages must be a comma-separated list of pages/ranges, e.g. 1,3,5-7" >&2
  exit 2
fi
for rule in "${fixture_page_selection_rules[@]}"; do
  if [[ "$rule" != *=* || "${rule##*=}" == "$rule" ]] || ! is_page_selection "${rule##*=}"; then
    echo "--fixture-pages must have the form 'glob=1,3,5-7'" >&2
    exit 2
  fi
done
for rule in "${fixture_page_limit_rules[@]}"; do
  if [[ "$rule" != *=* || "${rule##*=}" == "$rule" || ! "${rule##*=}" =~ ^[1-9][0-9]*$ ]]; then
    echo "--fixture-max-pages must have the form 'glob=positive_integer'" >&2
    exit 2
  fi
  fixture_page_selection_rules+=("${rule%=*}=1-${rule##*=}")
done
for rule in "${engine_profile_rules[@]}"; do
  if [[ "$rule" != *=* || -z "${rule%=*}" || -z "${rule#*=}" ]]; then
    echo "--engine-profile must have the form 'engine=profile'" >&2
    exit 2
  fi
done

IFS=',' read -r -a engines <<< "$engines_csv"
commit="$(git -C "$source_root" rev-parse HEAD)"
subject="$(git -C "$source_root" show -s --format=%s HEAD)"
ocr_tree="$(git -C "$source_root" rev-parse HEAD:ocr)"
runtime_image_id="$(docker image inspect --format '{{.Id}}' "$runtime_image")"
container_name="ittm-benchmark-${commit:0:8}-$$"
server_log="$output_root/server.log"
summary="$output_root/summary.tsv"
manifest="$output_root/manifest.md"
resources="$output_root/resources.tsv"
profile_flags_file="$output_root/profile-flags.tsv"
base_url=""
page_limited_dir=""
gpu_enabled=0
gpu_reason="disabled by --gpu off"
gpu_docker_args=()

docker_gpus_flag_available() {
  docker run --rm --gpus all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    --entrypoint sh "$runtime_image" -lc 'exit 0' >/dev/null 2>&1
}

docker_nvidia_runtime_available() {
  docker run --rm --runtime=nvidia \
    -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    --entrypoint sh "$runtime_image" -lc 'exit 0' >/dev/null 2>&1
}

enable_gpu_if_available() {
  if docker_gpus_flag_available; then
    gpu_enabled=1
    gpu_reason="$1 via --gpus all"
    gpu_docker_args=(--gpus all)
    return 0
  fi
  if docker_nvidia_runtime_available; then
    gpu_enabled=1
    gpu_reason="$1 via --runtime=nvidia"
    gpu_docker_args=(--runtime=nvidia)
    return 0
  fi
  return 1
}

case "$gpu_mode" in
  on)
    if ! enable_gpu_if_available "enabled by --gpu on"; then
      echo "Docker GPU runtime is not available for $runtime_image; use --gpu off to force CPU." >&2
      exit 2
    fi
    ;;
  auto)
    if command -v nvidia-smi >/dev/null 2>&1 &&
      nvidia-smi -L >/dev/null 2>&1 &&
      enable_gpu_if_available "enabled automatically"; then
      :
    else
      gpu_reason="not available to Docker"
    fi
    ;;
esac

stop_container() {
  if docker inspect "$container_name" >/dev/null 2>&1; then
    {
      printf '\n===== container stop %s =====\n' "$(date --iso-8601=seconds)"
      docker logs "$container_name"
    } >>"$server_log" 2>&1 || true
  fi
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}

cleanup() {
  stop_container
  if [[ -n "$page_limited_dir" ]]; then
    rm -rf "$page_limited_dir"
  fi
}
trap cleanup EXIT

page_selection_for_fixture() {
  local file_name="$1"
  local selected="$page_selection"
  local rule pattern pages
  for rule in "${fixture_page_selection_rules[@]}"; do
    pattern="${rule%=*}"
    pages="${rule##*=}"
    if [[ "$file_name" == $pattern ]]; then
      selected="$pages"
    fi
  done
  echo "$selected"
}

if [[ -n "$page_selection" || ${#fixture_page_selection_rules[@]} -gt 0 ]]; then
  if ! command -v qpdf >/dev/null 2>&1; then
    echo "qpdf is required for PDF page selection" >&2
    exit 2
  fi
  page_limited_dir="$(mktemp -d)"
  limited_fixtures=()
  for fixture in "${fixtures[@]}"; do
    if [[ "${fixture,,}" != *.pdf ]]; then
      limited_fixtures+=("$fixture")
      continue
    fi
    file_name="$(basename "$fixture")"
    selected_pages="$(page_selection_for_fixture "$file_name")"
    if [[ -z "$selected_pages" ]]; then
      limited_fixtures+=("$fixture")
      continue
    fi
    validate_page_selection_for_pdf "$fixture" "$selected_pages"
    limited_fixture="$page_limited_dir/$file_name"
    qpdf --empty --pages "$fixture" "$selected_pages" -- "$limited_fixture"
    limited_fixtures+=("$limited_fixture")
  done
  fixtures=("${limited_fixtures[@]}")
fi

: >"$server_log"

start_container() {
  docker_args=(-d --name "$container_name" "${gpu_docker_args[@]}")

  docker_env=(
    -e PORT=8000 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONPATH=/opt/ittm-python-packages \
    -e EASY_INSTALL_TARGET=/opt/ittm-python-packages \
    -e EASYOCR_MODULE_PATH=/models/easyocr
  )
  if [[ "$gpu_enabled" -eq 1 ]]; then
    docker_env+=(
      -e NVIDIA_VISIBLE_DEVICES=all
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    )
  else
    docker_env+=(-e CUDA_VISIBLE_DEVICES=)
  fi

  docker run "${docker_args[@]}" \
    "${docker_env[@]}" \
    -p 127.0.0.1::8000 \
    -v "$source_root/ocr:/app:ro" \
    -v "$python_packages_volume:/opt/ittm-python-packages" \
    -v "$models_volume:/models/easyocr" \
    -w /app \
    "$runtime_image" \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    >/dev/null

  port="$(docker port "$container_name" 8000/tcp | sed 's/.*://')"
  base_url="http://127.0.0.1:$port"

  ready=0
  for _ in $(seq 1 90); do
    if curl -fsS --max-time 2 "$base_url/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ $ready -ne 1 ]]; then
    echo "OCR service did not become ready. See $server_log" >&2
    return 1
  fi
}

start_container

cat >"$manifest" <<EOF
# OCR benchmark

- commit: \`$commit\`
- subject: $subject
- OCR tree: \`$ocr_tree\`
- runtime image: \`$runtime_image_id\`
- engines: \`$engines_csv\`
- pipeline profile: \`${pipeline_profile:-per-engine default}\`
- engine profile overrides: \`${engine_profile_rules[*]:-none}\`
- GPU: \`$gpu_mode\` ($gpu_reason)
- expected root: \`$expected_root\`
- PDF pages: \`${page_selection:-all}\`
- PDF fixture pages: \`${fixture_page_selection_rules[*]:-none}\`
- timeout per request: ${timeout_seconds}s
- endpoint: direct Python \`/convert\`
- fixtures: ${#fixtures[@]}
- command: \`$(printf '%q ' "$0" "${original_args[@]}")\`
EOF

if [[ $resume -ne 1 || ! -s "$summary" ]]; then
  printf 'commit\tengine\tpipeline\tfile\thttp_status\tcurl_exit\twall_ms\tbackend_elapsed_ms\tpages\tchunks\ttables_found\ttable_cells\tflags\n' >"$summary"
fi
if [[ $resume -ne 1 || ! -s "$resources" ]]; then
  printf 'commit\tengine\tfile\tcpu\tmemory\tblock_io\n' >"$resources"
fi

profile_for_engine() {
  local rule engine_name profile_name
  for rule in "${engine_profile_rules[@]}"; do
    engine_name="${rule%=*}"
    profile_name="${rule#*=}"
    if [[ "$1" == "$engine_name" ]]; then
      echo "$profile_name"
      return
    fi
  done
  if [[ -n "$pipeline_profile" ]]; then
    echo "$pipeline_profile"
    return
  fi
  case "$1" in
    auto) echo "backend_auto_standard" ;;
    tesseract) echo "backend_tesseract_standard" ;;
    easyocr) echo "backend_easyocr_standard" ;;
    *)
      echo "Unsupported engine: $1" >&2
      return 2
      ;;
  esac
}

profiles=()
for engine in "${engines[@]}"; do
  profiles+=("$(profile_for_engine "$engine")")
done
PYTHONPATH="$source_root/ocr" python3 - "$output_root/profiles.json" "$profile_flags_file" "${profiles[@]}" <<'PY'
import dataclasses
import json
import pathlib
import sys

from app.pipeline_config import resolve_pipeline_profile
from app.pipeline_flags import profile_flags_string

output_path = pathlib.Path(sys.argv[1])
flags_path = pathlib.Path(sys.argv[2])
profile_names = sys.argv[3:]
payload = {
    name: dataclasses.asdict(resolve_pipeline_profile("auto", name))
    for name in dict.fromkeys(profile_names)
}
output_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
with flags_path.open("w", encoding="utf-8") as output:
    for name in dict.fromkeys(profile_names):
        output.write(f"{name}\t{profile_flags_string(resolve_pipeline_profile('auto', name))}\n")
PY

declare -A profile_flags_by_name=()
while IFS=$'\t' read -r profile_name profile_flags; do
  profile_flags_by_name["$profile_name"]="$profile_flags"
done <"$profile_flags_file"

for engine in "${engines[@]}"; do
  profile="$(profile_for_engine "$engine")"
  profile_flags="${profile_flags_by_name[$profile]:-}"
  engine_dir="$output_root/$engine"
  mkdir -p "$engine_dir"

  for fixture in "${fixtures[@]}"; do
    file_name="$(basename "$fixture")"
    result_file="$engine_dir/$file_name.md"
    response_file="$engine_dir/$file_name.response.json"
    curl_error_file="$engine_dir/$file_name.curl-error.txt"
    if [[ $resume -eq 1 && -s "$result_file" ]]; then
      continue
    fi
    start_ms="$(date +%s%3N)"

    set +e
    http_status="$(
      curl --silent --show-error \
        --max-time "$timeout_seconds" \
        --output "$response_file" \
        --write-out '%{http_code}' \
        --form "file=@\"$fixture\"" \
        "$base_url/convert?engine_type=$engine&pipeline_profile=$profile" \
        2>"$curl_error_file"
    )"
    curl_exit=$?
    set -e

    wall_ms=$(( $(date +%s%3N) - start_ms ))
    resource_snapshot="$(
      docker stats --no-stream \
        --format '{{.CPUPerc}}\t{{.MemUsage}}\t{{.BlockIO}}' \
        "$container_name"
    )"
    printf '%s\t%s\t%s\t%s\n' \
      "$commit" "$engine" "$file_name" "$resource_snapshot" >>"$resources"
    python3 - \
      "$response_file" "$result_file" "$summary" "$commit" "$engine" \
      "$profile" "$file_name" "$http_status" "$curl_exit" "$wall_ms" \
      "$curl_error_file" "$profile_flags" <<'PY'
import json
import pathlib
import sys

(
    response_path,
    result_path,
    summary_path,
    commit,
    engine,
    profile,
    file_name,
    http_status,
    curl_exit,
    wall_ms,
    curl_error_path,
    profile_flags,
) = sys.argv[1:]

response_file = pathlib.Path(response_path)
curl_error = pathlib.Path(curl_error_path).read_text(
    encoding="utf-8", errors="replace"
).strip()
payload = {}
if response_file.exists():
    try:
        payload = json.loads(response_file.read_text(encoding="utf-8"))
    except Exception:
        payload = {
            "detail": response_file.read_text(
                encoding="utf-8", errors="replace"
            )
        }

meta = payload.get("meta") or {}
text = payload.get("markdown")
if not isinstance(text, str):
    text = payload.get("detail") or payload.get("error") or curl_error
if not isinstance(text, str):
    text = json.dumps(payload, ensure_ascii=False, indent=2)

header = (
    f"# {wall_ms} ms"
    f" (backend: {meta.get('elapsed_ms', 'n/a')} ms,"
    f" HTTP {http_status or '000'}, curl {curl_exit})"
)
pathlib.Path(result_path).write_text(
    f"{header}\n---\n{text.rstrip()}\n",
    encoding="utf-8",
)

fields = [
    commit,
    engine,
    meta.get("pipeline", profile),
    file_name,
    http_status or "000",
    curl_exit,
    wall_ms,
    meta.get("elapsed_ms", ""),
    meta.get("pages", ""),
    meta.get("chunks", ""),
    meta.get("tables_found", ""),
    meta.get("table_cells", ""),
    "; ".join(meta.get("flags") or []) or profile_flags,
]
with pathlib.Path(summary_path).open("a", encoding="utf-8") as output:
    output.write("\t".join(str(value).replace("\t", " ") for value in fields))
    output.write("\n")
PY

    rm -f "$response_file"
    if [[ ! -s "$curl_error_file" ]]; then
      rm -f "$curl_error_file"
    fi
    if [[ $curl_exit -ne 0 ]]; then
      stop_container
      start_container
    fi
  done
done

cleanup
trap - EXIT
python3 "$script_dir/debug_report.py" \
  --summary "$summary" \
  --output-root "$output_root" \
  --expected-root "$expected_root" \
  --markdown "$output_root/comparison.md" \
  --tables-root "$output_root/tables" \
  --csv "$output_root/comparison.csv"
printf 'Benchmark complete: %s\n' "$output_root"
