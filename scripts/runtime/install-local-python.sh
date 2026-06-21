#!/bin/bash
# install.sh - Sets up the isolated Python environment for EasyOCR

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

ENV_DIR="${PYTHON_ENV_DIR:-ocr/.venv}"
HOST_PYTHON="${HOST_PYTHON:-${PYTHON_BIN:-}}"

echo "=== Setup Isolation Environment for OCR ==="

if [ -z "$HOST_PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        HOST_PYTHON="python3"
    elif command -v python >/dev/null 2>&1; then
        HOST_PYTHON="python"
    else
        echo "Python 3.10+ is required but not installed. Aborting."
        exit 1
    fi
fi

if ! command -v "$HOST_PYTHON" >/dev/null 2>&1; then
    echo "$HOST_PYTHON is required but not installed. Aborting."
    exit 1
fi

if ! "$HOST_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
then
    echo "Python 3.10+ is required. Selected interpreter is too old: $HOST_PYTHON"
    exit 1
fi

if [ ! -d "$ENV_DIR" ]; then
    echo "[1/3] Creating virtual environment at $ENV_DIR..."
    "$HOST_PYTHON" -m venv "$ENV_DIR"
else
    echo "[1/3] Virtual environment $ENV_DIR already exists."
fi

# Determine requirements
REQ="ocr/requirements-light.txt"
if [ "${INSTALL_EASYOCR:-0}" = "1" ]; then
    REQ="ocr/requirements.txt"
fi

echo "[2/3] Installing dependencies from $REQ..."
"$ENV_DIR/bin/pip" install --disable-pip-version-check -r "$REQ"

echo "[3/3] Python environment is ready."
echo "You can now start the application via scripts/runtime/run-local.sh"
