#!/bin/bash
# Independent lightweight runner avoiding hard Node dependency if Bun is present

echo "Searching for free ports..."
PY_PORT="8000"
while lsof -Pi :$PY_PORT -sTCP:LISTEN -t >/dev/null ; do
    PY_PORT=$((PY_PORT+1))
done

GW_PORT="3000"
while lsof -Pi :$GW_PORT -sTCP:LISTEN -t >/dev/null ; do
    if [ "$GW_PORT" == "3000" ]; then 
        # Don't increment if inside a managed environment where PORT must be 3000
        # AI Studio proxy needs 3000 mostly
        break; 
    fi
    GW_PORT=$((GW_PORT+1))
done

# 1. Start Python OCR Service in background
echo "Starting Python OCR Service on port $PY_PORT..."
export OCR_URL="http://127.0.0.1:$PY_PORT"

if [ ! -d "ocr/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv ocr/.venv
fi

echo "Installing Python dependencies..."
ocr/.venv/bin/pip install -r ocr/requirements.txt

ocr/.venv/bin/python -m uvicorn app.main:app --app-dir ocr --port $PY_PORT &
PYTHON_PID=$!

# Wait for python to be ready
sleep 2

# 2. Start Gateway
echo "Starting Gateway..."
export PORT=$GW_PORT

if command -v bun &> /dev/null; then
    echo "Bun found! Starting Gateway via Bun adapter on port $PORT..."
    # No package installation required for Bun (runs TypeScript natively without node_modules)
    bun run gateway/src/adapters/bun.ts &
else
    echo "Bun not found. Checking for tsx/node..."
    if ! command -v tsx &> /dev/null; then
        echo "Installing Node.js dependencies for fallback..."
        npm install
    fi
    echo "Starting Node adapter on port $PORT..."
    npx tsx gateway/src/adapters/node.ts &
fi
GATEWAY_PID=$!

echo "======================================"
echo "SERVICES ARE RUNNING"
echo "Gateway: http://localhost:$GW_PORT"
echo "Python API: http://localhost:$PY_PORT/v1/health"
echo "======================================"

# Cleanup on exit
trap "kill $PYTHON_PID $GATEWAY_PID" EXIT
wait
