#!/usr/bin/env bash
set -euo pipefail

image="${OCR_TEST_IMAGE:-ittm-ocr-resource-tests}"
memory="${OCR_TEST_MEMORY:-768m}"
cpus="${OCR_TEST_CPUS:-2}"
iterations="${OCR_SOAK_ITERATIONS:-3}"

docker build \
  -f docker/ocr.Dockerfile \
  --target test \
  --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
  -t "$image" \
  ./ocr

for iteration in $(seq 1 "$iterations"); do
  echo "Resource test iteration ${iteration}/${iterations}"
  docker run --rm \
    --memory="$memory" \
    --memory-swap="$memory" \
    --cpus="$cpus" \
    --pids-limit=256 \
    -e RUN_GENERATED_FUZZ=1 \
    "$image" \
    python -m pytest \
      tests/test_generated_media_matrix.py \
      tests/test_visual_mutations.py \
      tests/test_upload_processing.py \
      -q
done
