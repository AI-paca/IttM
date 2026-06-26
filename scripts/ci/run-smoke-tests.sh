#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
python_bin="${PYTHON_BIN:-python}"
ocr_test_image="${OCR_TEST_IMAGE:-}"

cd "$project_root"

node --import tsx --test \
  gateway/src/core/routes.test.ts \
  gateway/src/core/handle.test.ts \
  gateway/src/services/staticFiles.test.ts

(
  if [[ -n "$ocr_test_image" ]]; then
    docker run --rm "$ocr_test_image" python -m pytest tests/api/test_main.py -q
  else
    cd ocr
    "$python_bin" -m pytest tests/api/test_main.py -q
  fi
)
