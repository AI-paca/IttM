#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"
python_bin="${PYTHON_BIN:-python}"

cd "$project_root"

node --import tsx --test \
  gateway/src/core/routes.test.ts \
  gateway/src/core/handle.test.ts \
  gateway/src/services/staticFiles.test.ts

(
  cd ocr
  "$python_bin" -m pytest tests/test_main.py -q
)
