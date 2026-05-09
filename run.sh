#!/bin/bash
# Independent lightweight runner avoiding hard Node dependency if Bun is present

echo "Searching for free ports..."
find_free_port() {
    local preferred="$1"
    python3 - "$preferred" <<'PY'
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

PY_PORT="${PY_PORT:-$(find_free_port 8000)}"
GW_PORT="${PORT:-${GW_PORT:-$(find_free_port 3000)}}"

# 1. Start Python OCR Service in background
echo "[RUNNER] Checking for Python environment..."
if [ ! -d "ocr/.venv" ]; then
    echo "[RUNNER] Creating virtual environment..."
    python3 -m venv ocr/.venv
fi

echo "[RUNNER] Installing Python dependencies..."
ocr/.venv/bin/pip install -r ocr/requirements.txt | grep -v "already satisfied"

echo "[RUNNER] Starting Python OCR Service on port $PY_PORT with DEBUG logs..."
export OCR_URL="http://127.0.0.1:$PY_PORT"
ocr/.venv/bin/python -m uvicorn app.main:app --app-dir ocr --port $PY_PORT --log-level debug &
PYTHON_PID=$!

# Wait for python to be ready
echo "[RUNNER] Waiting for Python service to warm up..."
sleep 3

echo "[RUNNER] Building frontend resources..."
if command -v bun &> /dev/null; then
    echo "[RUNNER] Using Bun for install/build..."
    bun install > /dev/null
    bun run build
else
    echo "[RUNNER] Using NPM for install/build..."
    npm ci > /dev/null
    npm run build
fi

# 2. Start Gateway
echo "[RUNNER] Starting Gateway..."
export PORT=$GW_PORT

if command -v bun &> /dev/null; then
    echo "[RUNNER] Bun found! Starting Gateway via Bun adapter on port $PORT..."
    bun run gateway/src/adapters/bun.ts &
else
    echo "[RUNNER] Bun not found. Starting Gateway via Node adapter..."
    export NODE_ENV=production
    node gateway/src/adapters/node.ts &
fi
GATEWAY_PID=$!

echo "======================================"
echo "SERVICES ARE RUNNING"
echo "Gateway: http://localhost:$GW_PORT"
echo "Python API: http://localhost:$PY_PORT/health"
echo "======================================"

# Cleanup on exit
trap "kill $PYTHON_PID $GATEWAY_PID" EXIT
wait
