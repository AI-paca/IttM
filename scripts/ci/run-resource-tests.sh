#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
cd "$project_root"

image="${OCR_TEST_IMAGE:-ittm-ocr-resource-tests}"
memory="${OCR_TEST_MEMORY:-768m}"
cpus="${OCR_TEST_CPUS:-2}"
iterations="${OCR_SOAK_ITERATIONS:-3}"
tier="${OCR_TEST_TIER:-resource}"

pytest_targets=(
  tests/quality/test_generated_media_matrix.py
  tests/quality/test_visual_mutations.py
  tests/api/test_upload_processing.py
)
docker_build_args=()
docker_env=(
  -e RUN_GENERATED_FUZZ=1
)

if [[ "$tier" == "quality" ]]; then
  iterations="${OCR_SOAK_ITERATIONS:-1}"
  docker_build_args+=(--build-arg OCR_INSTALL_CJK_FONTS=1)
  docker_env+=(
    -e RUN_OCR_QUALITY=1
  )
  pytest_targets=(
    tests/quality/test_ocr_quality.py::test_generated_functional_ocr_quality_matrix
    tests/quality/test_visual_mutations.py::test_tesseract_preserves_identifiers_across_visual_mutations
  )
elif [[ "$tier" != "resource" ]]; then
  echo "Unknown OCR_TEST_TIER '$tier' (expected 'resource' or 'quality')" >&2
  exit 2
fi

docker build \
  -f docker/ocr.Dockerfile \
  --target test \
  --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
  "${docker_build_args[@]}" \
  -t "$image" \
  ./ocr

for iteration in $(seq 1 "$iterations"); do
  echo "${tier^} test iteration ${iteration}/${iterations}"
  docker run --rm \
    --memory="$memory" \
    --memory-swap="$memory" \
    --cpus="$cpus" \
    --pids-limit=256 \
    "${docker_env[@]}" \
    "$image" \
    python -m pytest \
      "${pytest_targets[@]}" \
      -q
done
