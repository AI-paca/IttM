#!/bin/bash
# scripts/runtime/run-local.sh
# Starts the stable local Bun gateway + Python virtual environment.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_ENV_DIR="${PYTHON_ENV_DIR:-ocr/.venv}"
HOST_PYTHON="${HOST_PYTHON:-${PYTHON_BIN:-}}"
VENV_PYTHON="$PYTHON_ENV_DIR/bin/python"
PY_PORT_LOCKED=0
GW_PORT_LOCKED=0

if [ -n "${PY_PORT:-}" ]; then
    PY_PORT_LOCKED=1
fi
if [ -n "${PORT:-}" ]; then
    GW_PORT_LOCKED=1
fi

PY_PORT="${PY_PORT:-8000}"
GW_PORT="${PORT:-3000}"

PYTHON_PID=""
GATEWAY_PID=""

cleanup() {
    if [ -z "$GATEWAY_PID$PYTHON_PID" ]; then
        return
    fi

    echo "[RUNNER] Stopping services..."
    if [ -n "$GATEWAY_PID" ]; then
        kill "$GATEWAY_PID" 2>/dev/null || true
    fi
    if [ -n "$PYTHON_PID" ]; then
        kill "$PYTHON_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

find_free_port() {
    local preferred="$1"
    local host="${2:-127.0.0.1}"

    "$HOST_PYTHON" - "$host" "$preferred" <<'PY'
import socket
import sys

host = sys.argv[1]
preferred = int(sys.argv[2])


def can_bind(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
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

select_port() {
    local label="$1"
    local preferred="$2"
    local locked="$3"
    local selected

    if ! selected="$(find_free_port "$preferred" 127.0.0.1)"; then
        echo "[RUNNER] Could not check a free port for $label." >&2
        return 1
    fi

    if [ "$selected" = "$preferred" ]; then
        printf "%s" "$preferred"
        return 0
    fi

    if [ "$locked" -eq 1 ]; then
        echo "[RUNNER] Port $preferred for $label is busy, and the value is fixed." >&2
        echo "[RUNNER] Free the port or start with another value: $label=$selected" >&2
        return 1
    fi

    echo "[RUNNER] Port $preferred for $label is busy; using $selected." >&2
    printf "%s" "$selected"
}

wait_for_python_service() {
    echo "[RUNNER] Waiting for Python OCR Service at http://127.0.0.1:$PY_PORT/health..."
    for _ in $(seq 1 30); do
        if curl -s -f "http://127.0.0.1:$PY_PORT/health" > /dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "[RUNNER] Python OCR Service did not start."
    return 1
}

ensure_python_env() {
    if [ "${SKIP_PYTHON:-0}" = "1" ]; then
        echo "[RUNNER] SKIP_PYTHON=1, skipping Python OCR Service."
        return 1
    fi

    if [ ! -f "$VENV_PYTHON" ]; then
        echo "[RUNNER] Python venv was not found. Creating local environment ($PYTHON_ENV_DIR)..."
        HOST_PYTHON="$HOST_PYTHON" bash "$SCRIPT_DIR/install-local-python.sh"
    fi

    if ! "$VENV_PYTHON" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
        echo "[RUNNER] Python venv is incomplete. Reinstalling light dependencies..."
        HOST_PYTHON="$HOST_PYTHON" bash "$SCRIPT_DIR/install-local-python.sh"
    fi
}

ensure_system_tools() {
    if [ "${SKIP_PYTHON:-0}" = "1" ]; then
        return 0
    fi

    if ! command -v tesseract >/dev/null 2>&1; then
        echo "[RUNNER] tesseract was not found."
        echo "[RUNNER] Install the tesseract-ocr system package and language packages, then run the script again."
        return 1
    fi

    if ! command -v pdftoppm >/dev/null 2>&1; then
        echo "[RUNNER] pdftoppm was not found."
        echo "[RUNNER] Install the poppler-utils system package, then run the script again."
        return 1
    fi
}

ensure_bun_env() {
    if ! command -v bun >/dev/null 2>&1; then
        echo "[RUNNER] Bun was not found. Install Bun and run the script again."
        return 1
    fi

    if [ ! -d "node_modules" ]; then
        echo "[RUNNER] node_modules was not found. Installing JS dependencies with Bun..."
        bun install
    fi
}

ensure_host_python() {
    if [ -z "$HOST_PYTHON" ]; then
        if command -v python3 >/dev/null 2>&1; then
            HOST_PYTHON="python3"
        elif command -v python >/dev/null 2>&1; then
            HOST_PYTHON="python"
        else
            echo "[RUNNER] Python was not found. Install Python 3.10+ and run the script again."
            exit 1
        fi
    fi

    if ! command -v "$HOST_PYTHON" >/dev/null 2>&1; then
        echo "[RUNNER] $HOST_PYTHON was not found. Install Python 3.10+ or set HOST_PYTHON."
        exit 1
    fi

    if ! "$HOST_PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
        echo "[RUNNER] Python 3.10+ is required; the current $HOST_PYTHON is too old."
        exit 1
    fi
}

ensure_host_python

# Pick ports before launching anything so the printed URLs match reality.
PY_PORT="$(select_port PY_PORT "$PY_PORT" "$PY_PORT_LOCKED")"
GW_PORT="$(select_port PORT "$GW_PORT" "$GW_PORT_LOCKED")"

ensure_bun_env
ensure_system_tools

# 1. Start Python Service
if ensure_python_env; then
    echo "[RUNNER] Python venv: $PYTHON_ENV_DIR"
    echo "[RUNNER] Starting Python OCR Service on port $PY_PORT..."
    "$VENV_PYTHON" -m uvicorn app.main:app --app-dir ocr --host 127.0.0.1 --port "$PY_PORT" --log-level info &
    PYTHON_PID=$!
    wait_for_python_service
fi

# 2. Start Gateway / Web Server
echo "[RUNNER] Starting Gateway on port $GW_PORT..."
export PORT="$GW_PORT"
export OCR_URL="http://127.0.0.1:$PY_PORT"

echo "[RUNNER] Using Bun"
bun server.ts &
GATEWAY_PID=$!

echo "======================================"
echo "STABLE SERVICES ARE RUNNING LOCALLY:"
echo "Gateway / Web app: http://localhost:$GW_PORT"
if [ -n "$PYTHON_PID" ]; then
    echo "Python API: http://localhost:$PY_PORT"
    echo "EasyOCR install button will call: http://localhost:$GW_PORT/api/install-easyocr"
else
    echo "Python API: skipped. EasyOCR install and server OCR will not work."
fi
echo "======================================"

wait
