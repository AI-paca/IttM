#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/benchmark-testtables.sh \
    --source /path/to/commit-worktree \
    --fixtures /path/to/testtables \
    --output /path/to/testtables/tmp/<label> \
    [--engines auto,tesseract,easyocr] \
    [--fixture 'photo*.jpg'] \
    [--timeout 300] \
    [--resume]

The script starts the selected worktree's OCR service in Docker, calls the
direct /convert endpoint with curl, and writes one Markdown result per file and
engine. --fixture may be repeated and accepts shell glob patterns matched
against basenames. It stays quiet while OCR is running.
EOF
}

source_root=""
fixtures_root=""
output_root=""
engines_csv="auto,tesseract,easyocr"
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
    --output)
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
base_url=""

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
}
trap cleanup EXIT

: >"$server_log"

start_container() {
  docker run -d --name "$container_name" \
    -e PORT=8000 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONPATH=/opt/ittm-python-packages \
    -e EASY_INSTALL_TARGET=/opt/ittm-python-packages \
    -e EASYOCR_MODULE_PATH=/models/easyocr \
    -e CUDA_VISIBLE_DEVICES= \
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
- timeout per request: ${timeout_seconds}s
- endpoint: direct Python \`/convert\`
- fixtures: ${#fixtures[@]}
EOF

if [[ $resume -ne 1 || ! -s "$summary" ]]; then
  printf 'commit\tengine\tfile\thttp_status\tcurl_exit\twall_ms\tbackend_elapsed_ms\tpages\tchunks\ttables_found\ttable_cells\n' >"$summary"
fi
if [[ $resume -ne 1 || ! -s "$resources" ]]; then
  printf 'commit\tengine\tfile\tcpu\tmemory\tblock_io\n' >"$resources"
fi

profile_for_engine() {
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

for engine in "${engines[@]}"; do
  profile="$(profile_for_engine "$engine")"
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
      "$file_name" "$http_status" "$curl_exit" "$wall_ms" \
      "$curl_error_file" <<'PY'
import json
import pathlib
import sys

(
    response_path,
    result_path,
    summary_path,
    commit,
    engine,
    file_name,
    http_status,
    curl_exit,
    wall_ms,
    curl_error_path,
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
    file_name,
    http_status or "000",
    curl_exit,
    wall_ms,
    meta.get("elapsed_ms", ""),
    meta.get("pages", ""),
    meta.get("chunks", ""),
    meta.get("tables_found", ""),
    meta.get("table_cells", ""),
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
printf 'Benchmark complete: %s\n' "$output_root"
