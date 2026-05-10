#!/bin/bash
# Lightweight local runner. Bun path is the trusted low-RAM path:
# no tests, no Docker, no EasyOCR/PyTorch unless explicitly requested.

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
PY_PORT=""
GW_PORT=""
PYTHON_PID=""
GATEWAY_PID=""

cleanup() {
    if [ -n "${GATEWAY_PID:-}" ]; then
        kill "$GATEWAY_PID" 2>/dev/null || true
    fi
    if [ -n "${PYTHON_PID:-}" ]; then
        kill "$PYTHON_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

has_command() {
    command -v "$1" >/dev/null 2>&1
}

find_free_port() {
    local preferred="$1"
    "$PYTHON_BIN" - "$preferred" <<'PY'
import socket
import sys

preferred = int(sys.argv[1])


def can_bind(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


for candidate in list(range(preferred, preferred + 100)) + list(range(49152, 61000)):
    if can_bind(candidate):
        print(candidate)
        sys.exit(0)

print(f"No free port found near {preferred}", file=sys.stderr)
sys.exit(1)
PY
}

has_tesseract_lang() {
    local lang="$1"
    has_command tesseract && tesseract --list-langs 2>/dev/null | grep -qx "$lang"
}

install_apt_packages() {
    local packages=("$@")

    if [ "${#packages[@]}" -eq 0 ]; then
        return 0
    fi

    if ! has_command apt-get; then
        echo "[RUNNER] Missing system packages: ${packages[*]}"
        echo "[RUNNER] Install the matching packages for your distro, then rerun: bash run.sh"
        return 1
    fi

    echo "[RUNNER] Installing missing system packages: ${packages[*]}"
    if [ "$(id -u)" = "0" ]; then
        apt-get update
        apt-get install -y "${packages[@]}"
    elif has_command sudo && sudo -n true 2>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y "${packages[@]}"
    else
        echo "[RUNNER] Need root/sudo to install: ${packages[*]}"
        echo "[RUNNER] Run: sudo apt-get update && sudo apt-get install -y ${packages[*]}"
        return 1
    fi
}

ensure_system_dependencies() {
    if [ "${SKIP_SYSTEM_DEPS:-0}" = "1" ]; then
        echo "[RUNNER] Skipping system dependency checks because SKIP_SYSTEM_DEPS=1"
        return 0
    fi

    local required_packages=()
    local optional_packages=()

    if ! has_command tesseract; then
        required_packages+=("tesseract-ocr")
    fi
    if ! has_command pdftoppm; then
        required_packages+=("poppler-utils")
    fi

    if ! has_tesseract_lang eng; then
        required_packages+=("tesseract-ocr-eng")
    fi
    if ! has_tesseract_lang rus; then
        required_packages+=("tesseract-ocr-rus")
    fi
    if ! has_tesseract_lang chi_sim; then
        optional_packages+=("tesseract-ocr-chi-sim")
    fi

    install_apt_packages "${required_packages[@]}"

    if [ "${#optional_packages[@]}" -gt 0 ]; then
        if has_command apt-get && { [ "$(id -u)" = "0" ] || { has_command sudo && sudo -n true 2>/dev/null; }; }; then
            install_apt_packages "${optional_packages[@]}"
        else
            echo "[RUNNER] Optional Tesseract language data is missing: chi_sim."
            echo "[RUNNER] Continuing with installed languages; OCR quality job installs chi_sim in CI."
        fi
    fi
}

ensure_python_environment() {
    echo "[RUNNER] Checking Python environment..."
    if [ ! -d "ocr/.venv" ]; then
        echo "[RUNNER] Creating virtual environment..."
        if ! "$PYTHON_BIN" -m venv ocr/.venv; then
            echo "[RUNNER] Could not create ocr/.venv."
            echo "[RUNNER] On Debian/Ubuntu install python3-venv, then rerun: bash run.sh"
            return 1
        fi
    fi

    local requirements="${OCR_REQUIREMENTS:-ocr/requirements-light.txt}"
    if [ "${INSTALL_EASYOCR:-0}" = "1" ]; then
        requirements="ocr/requirements.txt"
        echo "[RUNNER] INSTALL_EASYOCR=1: using full OCR requirements with EasyOCR/PyTorch."
    fi

    echo "[RUNNER] Installing Python runtime dependencies from $requirements..."
    ocr/.venv/bin/python -m pip install --disable-pip-version-check -r "$requirements"
}

wait_for_python_service() {
    echo "[RUNNER] Waiting for Python service to become ready..."
    local health_url="http://127.0.0.1:$PY_PORT/health"

    for _ in $(seq 1 30); do
        if ocr/.venv/bin/python - "$health_url" <<'PY'
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=1) as response:
        sys.exit(0 if 200 <= response.status < 500 else 1)
except Exception:
    sys.exit(1)
PY
        then
            return 0
        fi
        sleep 1
    done

    echo "[RUNNER] Python service did not become ready at $health_url"
    return 1
}

build_frontend() {
    export VITE_BASE_PATH="/"

    if [ -f "dist/index.html" ] && [ "${FORCE_BUILD:-0}" != "1" ] && { has_command bun || [ -x "./node_modules/.bin/tsx" ]; }; then
        echo "[RUNNER] Reusing existing dist/. Set FORCE_BUILD=1 to rebuild frontend."
        return 0
    fi

    echo "[RUNNER] Building frontend resources..."
    if has_command bun; then
        echo "[RUNNER] Using Bun for install/build. Tests are not run in lightweight mode."
        if [ -f "bun.lock" ]; then
            bun install --frozen-lockfile
        else
            bun install
        fi
        bun run build
    else
        echo "[RUNNER] Bun not found. Using Node/npm fallback for install/build."
        npm ci
        npm run build
    fi
}

start_python_service() {
    echo "[RUNNER] Starting Python OCR Service on port $PY_PORT..."
    export OCR_URL="http://127.0.0.1:$PY_PORT"
    ocr/.venv/bin/python -m uvicorn app.main:app --app-dir ocr --port "$PY_PORT" --log-level info &
    PYTHON_PID=$!
}

start_gateway() {
    echo "[RUNNER] Starting Gateway..."
    export PORT="$GW_PORT"

    if has_command bun; then
        echo "[RUNNER] Bun found. Starting Gateway via Bun adapter on port $PORT..."
        bun run gateway/src/adapters/bun.ts &
    else
        echo "[RUNNER] Bun not found. Starting Gateway via Node adapter on port $PORT..."
        export NODE_ENV=production
        ./node_modules/.bin/tsx gateway/src/adapters/node.ts &
    fi
    GATEWAY_PID=$!
}

main() {
    echo "[RUNNER] Searching for free ports..."
    PY_PORT="${PY_PORT:-$(find_free_port 8000)}"
    GW_PORT="${PORT:-${GW_PORT:-$(find_free_port 3000)}}"

    ensure_system_dependencies
    ensure_python_environment
    start_python_service
    wait_for_python_service
    build_frontend
    start_gateway

    echo "======================================"
    echo "SERVICES ARE RUNNING"
    echo "Gateway: http://localhost:$GW_PORT"
    echo "Python API: http://localhost:$PY_PORT/health"
    echo "======================================"

    wait
}

main "$@"
