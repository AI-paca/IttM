#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
python_bin="${PYTHON_BIN:-python}"
ocr_test_image="${OCR_TEST_IMAGE:-}"

cd "$project_root"

node --import tsx --test \
  gateway/src/tasks/task-service.test.ts \
  gateway/src/tasks/process-worker.test.ts \
  gateway/src/tasks/input-storage.test.ts \
  web/src/ocr/layout-pipeline.test.ts \
  web/src/ocr/pipeline-config.test.ts \
  web/src/ocr/layout-stages.test.ts \
  web/src/ocr/layout-selectors.test.ts

(
  if [[ -n "$ocr_test_image" ]]; then
    docker run --rm "$ocr_test_image" python -m pytest \
      tests/quality/test_generated_fixture_registry.py \
      tests/api/test_upload_processing.py \
      -q
  else
    cd ocr
    "$python_bin" -m pytest \
      tests/quality/test_generated_fixture_registry.py \
      tests/api/test_upload_processing.py \
      -q
  fi
)
