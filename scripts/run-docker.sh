#!/bin/bash
# scripts/run-docker.sh
# Podnimayet tyazheluyu artilleriyu v izolirovannyh konteynerah.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v docker >/dev/null 2>&1; then
    echo "[RUNNER] Docker ne ustanovlen."
    exit 1
fi

export GATEWAY_HOST_BIND="${GATEWAY_HOST_BIND:-127.0.0.1}"
export GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-3000}"
export NGINX_INTERNAL_PORT="${NGINX_INTERNAL_PORT:-80}"
export OCR_INTERNAL_PORT="${OCR_INTERNAL_PORT:-8000}"
export OCR_REQUIREMENTS="${OCR_REQUIREMENTS:-requirements-light.txt}"
GATEWAY_HOST_PORT_LOCKED=0
BUILD_IMAGES_NO_BUILD=0
if [ -n "${GATEWAY_HOST_PORT:-}" ]; then
    GATEWAY_HOST_PORT_LOCKED=1
fi

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

current_gateway_port() {
    docker compose port nginx "$NGINX_INTERNAL_PORT" 2>/dev/null | awk -F: 'NF > 1 { print $NF; exit }' || true
}

if [ "$GATEWAY_HOST_PORT_LOCKED" -eq 0 ]; then
    GATEWAY_HOST_PORT="$(current_gateway_port)"
    if [ -z "$GATEWAY_HOST_PORT" ]; then
        GATEWAY_HOST_PORT="$(find_free_port 3000 "$GATEWAY_HOST_BIND")"
    fi
fi
export GATEWAY_HOST_PORT

if [ "$GATEWAY_HOST_PORT_LOCKED" -eq 0 ] && [ "$GATEWAY_HOST_PORT" != "3000" ]; then
    echo "[RUNNER] Port 3000 zanyat, Docker Web app budet na http://localhost:$GATEWAY_HOST_PORT"
fi

wait_for_gateway() {
    local url="http://127.0.0.1:$GATEWAY_HOST_PORT/api/health"
    echo "[RUNNER] Ozhidanie Gateway stack: $url"
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    echo "[RUNNER] Gateway stack ne otvetil. Logi:"
    docker compose logs --tail=120 nginx gateway ocr || true
    return 1
}

has_local_compose_images() {
    docker image inspect ittm-nginx:latest ittm-gateway:latest ittm-ocr:latest >/dev/null 2>&1
}

is_registry_error() {
    local log_file="$1"
    grep -Eiq 'registry-1\.docker\.io|auth\.docker\.io|registry\.npmjs\.org|failed to do request|failed to authorize|error reading from server: EOF|unexpected EOF|ECONNRESET|ETIMEDOUT|network request to|Client network socket disconnected|TLS handshake timeout|temporary failure in name resolution|network is unreachable|i/o timeout' "$log_file"
}

create_gateway_fallback_image() {
    echo "[RUNNER] Sobirayu fallback gateway image iz lokalnyh dist/ i node_modules."
    npm run build:server

    local container_id
    container_id="$(docker create node:22-slim sh -c "node dist/server.js")"
    docker cp package.json "$container_id:/package.json"
    docker cp node_modules "$container_id:/node_modules"
    docker cp dist "$container_id:/dist"
    docker commit \
        --change "WORKDIR /" \
        --change "ENV PORT=$GATEWAY_INTERNAL_PORT" \
        --change "ENV NODE_ENV=production" \
        --change "EXPOSE $GATEWAY_INTERNAL_PORT" \
        --change 'CMD ["node", "dist/server.js"]' \
        "$container_id" ittm-gateway:latest >/dev/null
    docker rm -f "$container_id" >/dev/null
}

refresh_nginx_fallback_image() {
    echo "[RUNNER] Obnovlyayu fallback nginx image iz lokalnogo dist/."
    npm run build:web

    local container_id
    container_id="$(docker create ittm-nginx:latest)"
    docker cp dist/. "$container_id:/usr/share/nginx/html/"
    docker commit "$container_id" ittm-nginx:latest >/dev/null
    docker rm -f "$container_id" >/dev/null
}

refresh_ocr_fallback_image() {
    echo "[RUNNER] Obnovlyayu fallback OCR image iz lokalnogo ocr/app/."

    local container_id
    container_id="$(docker create ittm-ocr:latest)"
    docker cp ocr/app/. "$container_id:/app/app/"
    docker cp ocr/requirements.txt "$container_id:/app/requirements.txt"
    docker cp ocr/requirements-light.txt "$container_id:/app/requirements-light.txt"
    docker commit "$container_id" ittm-ocr:latest >/dev/null
    docker rm -f "$container_id" >/dev/null
}

refresh_fallback_images() {
    refresh_ocr_fallback_image
    create_gateway_fallback_image
    refresh_nginx_fallback_image
}

build_images() {
    if [ "${SKIP_DOCKER_BUILD:-0}" = "1" ]; then
        echo "[RUNNER] SKIP_DOCKER_BUILD=1, ispolzuyu lokalnye Docker images."
        BUILD_IMAGES_NO_BUILD=1
        return 0
    fi

    if [ "${FAST_DOCKER_RESTART:-0}" = "1" ] && [ "$OCR_REQUIREMENTS" = "requirements-light.txt" ] && [ "${FULL_DOCKER_BUILD:-0}" != "1" ] && [ "${REBUILD:-0}" != "1" ] && has_local_compose_images; then
        echo "[RUNNER] Lokalnye ittm-* images naideni."
        echo "[RUNNER] Obnovlyayu gateway/nginx iz lokalnoy sborki bez obrashcheniya k Docker Hub."
        refresh_fallback_images
        BUILD_IMAGES_NO_BUILD=1
        return 0
    fi

    local log_file
    log_file="$(mktemp)"
    local build_args=(docker compose build)
    if [ "${REBUILD:-0}" = "1" ]; then
        build_args+=(--no-cache)
    fi

    echo "[RUNNER] Soberu gateway otdelno: on osnovan na lokalnom node:22-slim, kogda Docker Hub shataetsya."
    set +e
    "${build_args[@]}" gateway 2>&1 | tee "$log_file"
    local gateway_status=${PIPESTATUS[0]}
    set -e
    if [ "$gateway_status" -ne 0 ]; then
        if is_registry_error "$log_file" && [ "$OCR_REQUIREMENTS" = "requirements-light.txt" ] && has_local_compose_images; then
            echo "[RUNNER] Setevoy sboy pri sborke gateway. Peresobirayu fallback images lokalno."
            refresh_fallback_images
            rm -f "$log_file"
            BUILD_IMAGES_NO_BUILD=1
            return 0
        fi
        rm -f "$log_file"
        return 1
    fi
    : > "$log_file"

    set +e
    "${build_args[@]}" 2>&1 | tee "$log_file"
    local status=${PIPESTATUS[0]}
    set -e

    if [ "$status" -eq 0 ]; then
        rm -f "$log_file"
        return 0
    fi

    if is_registry_error "$log_file" && [ "$OCR_REQUIREMENTS" = "requirements-light.txt" ] && has_local_compose_images; then
        echo "[RUNNER] Docker/npm registry nedostupen, no lokalnye ittm-* images est."
        refresh_nginx_fallback_image
        echo "[RUNNER] Podnimayu stack bez rebuild: docker compose up -d --no-build"
        rm -f "$log_file"
        BUILD_IMAGES_NO_BUILD=1
        return 0
    fi

    rm -f "$log_file"
    return "$status"
}

echo "[RUNNER] Zapusk docker-compose..."
build_images
docker compose up -d --no-build --force-recreate --remove-orphans
wait_for_gateway

echo "======================================"
echo "DOCKER SERVISY ZAPUSHCHENY:"
echo "Web app: http://localhost:$GATEWAY_HOST_PORT"
echo "Health:  http://localhost:$GATEWAY_HOST_PORT/api/health"
echo "Gateway is private inside Docker; use the Web app URL above."
echo "OCR backend is private inside Docker at http://ocr:$OCR_INTERNAL_PORT"
echo "======================================"
echo "[RUNNER] Dlya prosmotra logov ispolzuyte:"
echo "docker compose logs -f"
