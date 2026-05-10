#!/bin/bash

# Локальная отладка, максимально близкая к .github/workflows/tests.yml.
set -euo pipefail

CLEAN=0
RUN_DOCKER=1
RUN_ACT=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER_NOTIFY_SCRIPT="$SCRIPT_DIR/notify-docker-restart.sh"
OCR_HOST_PORT_LOCKED=0
GATEWAY_HOST_PORT_LOCKED=0

cd "$PROJECT_ROOT"

if [ -n "${OCR_HOST_PORT:-}" ]; then
  OCR_HOST_PORT_LOCKED=1
fi
if [ -n "${GATEWAY_HOST_PORT:-}" ]; then
  GATEWAY_HOST_PORT_LOCKED=1
fi

for arg in "$@"; do
  case "$arg" in
    --clean)
      CLEAN=1
      ;;
    --no-docker)
      RUN_DOCKER=0
      ;;
    --no-act)
      RUN_ACT=0
      ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: bash scripts/debug.sh [--clean] [--no-docker] [--no-act]"
      exit 2
      ;;
  esac
done

trap 'echo -e "\a"; command -v notify-send &> /dev/null && notify-send -u critical "Отладка упала" "Скрипт прервался, проверь консоль."' ERR

call_for_docker_restart() {
  local reason="$1"
  if [ -f "$DOCKER_NOTIFY_SCRIPT" ]; then
    bash "$DOCKER_NOTIFY_SCRIPT" "$reason"
  else
    printf '\a'
    echo "$reason"
    if [ -t 0 ]; then
      read -r -p "Перезапусти Docker руками и нажми Enter..."
    else
      sleep 30
    fi
  fi
}

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

assign_compose_ports() {
  local force="${1:-0}"

  export OCR_HOST_BIND="${OCR_HOST_BIND:-127.0.0.1}"
  export GATEWAY_HOST_BIND="${GATEWAY_HOST_BIND:-127.0.0.1}"

  if [ "$OCR_HOST_PORT_LOCKED" -eq 0 ] && { [ "$force" -eq 1 ] || [ -z "${OCR_HOST_PORT:-}" ]; }; then
    OCR_HOST_PORT="$(find_free_port 8000 "$OCR_HOST_BIND")"
    export OCR_HOST_PORT
  fi

  if [ "$GATEWAY_HOST_PORT_LOCKED" -eq 0 ] && { [ "$force" -eq 1 ] || [ -z "${GATEWAY_HOST_PORT:-}" ]; }; then
    GATEWAY_HOST_PORT="$(find_free_port 3000 "$GATEWAY_HOST_BIND")"
    export GATEWAY_HOST_PORT
  fi
}

run_docker_step() {
  local label="$1"
  shift
  local attempt=1
  local max_attempts=3
  local log_file

  while true; do
    log_file="$(mktemp)"
    set +e
    "$@" 2>&1 | tee "$log_file"
    local status=${PIPESTATUS[0]}
    set -e

    if [ "$status" -eq 0 ]; then
      rm -f "$log_file"
      return 0
    fi

    if grep -Eiq 'permission denied while trying to connect to the docker API|permission denied .*docker.sock' "$log_file"; then
      echo "Docker step failed because Docker socket access is denied, restart is unlikely to help: $label"
      rm -f "$log_file"
      return "$status"
    fi

    if grep -Eiq 'port is already allocated|Bind for [^ ]+ failed' "$log_file"; then
      if [ "$OCR_HOST_PORT_LOCKED" -eq 1 ] || [ "$GATEWAY_HOST_PORT_LOCKED" -eq 1 ]; then
        echo "Docker step failed because a fixed host port is busy; unset OCR_HOST_PORT/GATEWAY_HOST_PORT or choose free values: $label"
        rm -f "$log_file"
        return "$status"
      fi
      if [ "$attempt" -ge "$max_attempts" ]; then
        echo "Docker step failed after $attempt dynamic port attempt(s): $label"
        rm -f "$log_file"
        return "$status"
      fi
      echo "Host port collision during $label; selecting new compose host ports."
      assign_compose_ports 1
      echo "Using OCR host port ${OCR_HOST_PORT}, gateway host port ${GATEWAY_HOST_PORT}."
      rm -f "$log_file"
      attempt=$((attempt + 1))
      continue
    fi

    if ! grep -Eiq 'Cannot connect to the Docker daemon|Is the docker daemon running|docker daemon is not running|connection refused|connection reset by peer|TLS handshake timeout|i/o timeout|network is unreachable|temporary failure in name resolution|proxyconnect tcp|server misbehaving|no route to host' "$log_file"; then
      echo "Docker step failed with a real command error, not a daemon restart case: $label"
      rm -f "$log_file"
      return "$status"
    fi

    rm -f "$log_file"
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "Docker daemon/network step failed after $attempt attempt(s): $label"
      return "$status"
    fi
    call_for_docker_restart "Docker step failed: $label"
    attempt=$((attempt + 1))
  done
}

wait_for_ocr_service() {
  local attempt

  for attempt in 1 2; do
    echo "Waiting for OCR service on http://127.0.0.1:${OCR_HOST_PORT}/health..."
    for i in {1..20}; do
      if curl -s "http://127.0.0.1:${OCR_HOST_PORT}/health" | grep -q '"ok":true'; then
        echo "OCR service is ready."
        return 0
      fi
      sleep 2
    done

    docker compose logs ocr || true
    call_for_docker_restart "OCR container did not become healthy. Corporate firewall/Docker daemon may need a manual restart."
    run_docker_step "docker compose up -d after manual Docker restart" docker compose up -d
  done

  echo "OCR service did not become healthy after manual restart retry."
  return 1
}

has_tessdata() {
  local dir="$1"
  [ -f "$dir/eng.traineddata" ] && [ -f "$dir/rus.traineddata" ] && [ -f "$dir/chi_sim.traineddata" ]
}

prepare_browser_tessdata() {
  if [ -n "${BROWSER_OCR_LANG_PATH:-}" ] && has_tessdata "$BROWSER_OCR_LANG_PATH"; then
    return 0
  fi

  for dir in /usr/share/tesseract-ocr/5/tessdata /usr/share/tesseract-ocr/4.00/tessdata; do
    if has_tessdata "$dir"; then
      export BROWSER_OCR_LANG_PATH="$dir"
      return 0
    fi
  done

  if [ "$RUN_DOCKER" -eq 1 ]; then
    local target="$PROJECT_ROOT/.cache/tessdata"
    mkdir -p "$target"
    if ! has_tessdata "$target"; then
      run_docker_step "copy eng traineddata from OCR container" docker compose cp ocr:/usr/share/tesseract-ocr/5/tessdata/eng.traineddata "$target/"
      run_docker_step "copy rus traineddata from OCR container" docker compose cp ocr:/usr/share/tesseract-ocr/5/tessdata/rus.traineddata "$target/"
      run_docker_step "copy chi_sim traineddata from OCR container" docker compose cp ocr:/usr/share/tesseract-ocr/5/tessdata/chi_sim.traineddata "$target/"
    fi
    export BROWSER_OCR_LANG_PATH="$target"
  fi
}

echo "=== Start local debug ==="

if [ "$RUN_DOCKER" -eq 1 ]; then
  if ! command -v docker &> /dev/null; then
    echo "Docker не установлен или не запущен."
    exit 1
  fi

  echo "--- Docker compose ---"
  assign_compose_ports
  echo "Docker host ports: OCR http://127.0.0.1:${OCR_HOST_PORT}, gateway http://127.0.0.1:${GATEWAY_HOST_PORT}"
  if [ "$CLEAN" -eq 1 ]; then
    run_docker_step "docker compose down -v --remove-orphans" docker compose down -v --remove-orphans
    run_docker_step "docker system prune -f --volumes" docker system prune -f --volumes
    run_docker_step "docker compose build --no-cache" docker compose build --no-cache
  else
    run_docker_step "docker compose down --remove-orphans" docker compose down --remove-orphans
    run_docker_step "docker compose build" docker compose build
  fi
  run_docker_step "docker compose up -d" docker compose up -d
  wait_for_ocr_service

  echo "--- Python lint and tests inside OCR container ---"
  run_docker_step "docker compose exec ocr flake8" docker compose exec -T ocr python -m flake8 .
  run_docker_step "docker compose exec ocr black check" docker compose exec -T ocr python -m black --check .
  run_docker_step "docker compose exec ocr ruff check" docker compose exec -T ocr python -m ruff check .
  run_docker_step "docker compose exec ocr pytest" docker compose exec -T ocr pytest tests/ -q
fi

echo "--- Node checks ---"
npm ci --no-audit --progress=false --prefer-offline --fetch-retries=1 --fetch-timeout=30000
npm run format:check
npm run lint
npm test
npm run build

if [ "$RUN_DOCKER" -eq 1 ]; then
  echo "--- Strict OCR fixtures and backend quality tests inside OCR container ---"
  run_docker_step "generate OCR fixtures inside container" docker compose exec -T ocr python tests/quality_fixtures.py
  mkdir -p ocr/tests/fixtures
  run_docker_step "copy OCR fixtures from container" docker compose cp ocr:/app/tests/fixtures/. ocr/tests/fixtures/
  run_docker_step "strict backend OCR quality tests" docker compose exec -T -e RUN_OCR_QUALITY=1 ocr pytest tests/test_ocr_quality.py -q
else
  echo "--- Docker disabled: Python/OCR backend tests are expected to run through GitHub Actions/act ---"
fi

echo "--- Browser OCR quality test ---"
prepare_browser_tessdata
npm run test:ocr:browser

if [ "$RUN_ACT" -eq 1 ]; then
  echo "--- GitHub Actions through act ---"
  if ! command -v act &> /dev/null; then
    echo "act не найден; пропускаю локальный прогон workflow."
  else
    act -W .github/workflows/tests.yml
  fi
fi

echo "=== Debug complete ==="
echo "Для завершения контейнеров используйте: docker compose down"

echo -e "\a"
if command -v notify-send &> /dev/null; then
  notify-send -u normal "Отладка завершена" "Проверки прошли."
fi
