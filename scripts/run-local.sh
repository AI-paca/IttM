#!/bin/bash
# scripts/run-local.sh
# Zapusk STABLE varsii: lokalniy Node.js/Bun Gateway + Python VENV

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_ENV_DIR="${PYTHON_ENV_DIR:-ocr/.venv}"
PYTHON_BIN="$PYTHON_ENV_DIR/bin/python"
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
    echo "[RUNNER] Ostanovka servisov..."
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

    python3 - "$host" "$preferred" <<'PY'
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
        echo "[RUNNER] Ne udalos proverit svobodnyy port dlya $label." >&2
        return 1
    fi

    if [ "$selected" = "$preferred" ]; then
        printf "%s" "$preferred"
        return 0
    fi

    if [ "$locked" -eq 1 ]; then
        echo "[RUNNER] Port $preferred dlya $label zanyat, a znachenie zafiksirovano." >&2
        echo "[RUNNER] Osvobodi port ili zapusti s drugim znacheniem: $label=$selected" >&2
        return 1
    fi

    echo "[RUNNER] Port $preferred dlya $label zanyat, ispolzuyu $selected." >&2
    printf "%s" "$selected"
}

wait_for_python_service() {
    echo "[RUNNER] Ozhidanie Python OCR Service na http://127.0.0.1:$PY_PORT/health..."
    for _ in $(seq 1 30); do
        if curl -s -f "http://127.0.0.1:$PY_PORT/health" > /dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "[RUNNER] Python OCR Service ne smog zapustitsya."
    return 1
}

ensure_python_env() {
    if [ "${SKIP_PYTHON:-0}" = "1" ]; then
        echo "[RUNNER] SKIP_PYTHON=1, Python OCR Service bydet propushen."
        return 1
    fi

    if [ ! -f "$PYTHON_BIN" ]; then
        echo "[RUNNER] Python venv ne naiden. Sozdayu lokalnoe okruzhenie ($PYTHON_ENV_DIR)..."
        bash "$SCRIPT_DIR/install-local-python.sh"
    fi

    if ! "$PYTHON_BIN" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
        echo "[RUNNER] Python venv ne polnostyu gotov. Pereustanavlivayu light dependencies..."
        bash "$SCRIPT_DIR/install-local-python.sh"
    fi
}

# Pick ports before launching anything so the printed URLs match reality.
PY_PORT="$(select_port PY_PORT "$PY_PORT" "$PY_PORT_LOCKED")"
GW_PORT="$(select_port PORT "$GW_PORT" "$GW_PORT_LOCKED")"

# 1. Start Python Service
if ensure_python_env; then
    echo "[RUNNER] Python venv: $PYTHON_ENV_DIR"
    echo "[RUNNER] Zapusk Python OCR Service na portu $PY_PORT..."
    "$PYTHON_BIN" -m uvicorn app.main:app --app-dir ocr --host 127.0.0.1 --port "$PY_PORT" --log-level info &
    PYTHON_PID=$!
    wait_for_python_service
fi

# 2. Start Gateway / Web Server
echo "[RUNNER] Zapusk Gateway na portu $GW_PORT..."
export PORT="$GW_PORT"
export OCR_URL="http://127.0.0.1:$PY_PORT"

if command -v bun >/dev/null 2>&1; then
    echo "[RUNNER] Ispolzuyetsya Bun"
    bun server.ts &
    GATEWAY_PID=$!
elif command -v npx >/dev/null 2>&1; then
    echo "[RUNNER] Ispolzuyetsya Node (tsx)"
    npx tsx server.ts &
    GATEWAY_PID=$!
else
    echo "[RUNNER] Bun i npx ne naideny! Otmena."
    exit 1
fi

echo "======================================"
echo "STABLE SERVISY ZAPUSHCHENY LOKALNO:"
echo "Gateway / Web app: http://localhost:$GW_PORT"
if [ -n "$PYTHON_PID" ]; then
    echo "Python API: http://localhost:$PY_PORT"
    echo "EasyOCR install button will call: http://localhost:$GW_PORT/api/install-easyocr"
else
    echo "Python API: skipped. EasyOCR install and server OCR will not work."
fi
echo "======================================"

wait
