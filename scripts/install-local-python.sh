#!/bin/bash
# install.sh - Sets up the isolated Python environment for EasyOCR

set -euo pipefail

ENV_DIR="${PYTHON_ENV_DIR:-ocr/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "=== Setup Isolation Environment for OCR ==="

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but not installed. Aborting."
    exit 1
fi

if [ ! -d "$ENV_DIR" ]; then
    echo "[1/3] Creating virtual environment at $ENV_DIR..."
    "$PYTHON_BIN" -m venv "$ENV_DIR"
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
echo "You can now start the application via scripts/run-local.sh"
