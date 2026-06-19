#!/bin/bash
# scripts/runtime/build-lite.sh
# Kompiliruet statiku Lite versii dlya Github Pages ili Rasshireniya.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "[BUILD-LITE] Sobirayem LITE statiku cherez Vite..."
npm ci --no-audit --progress=false --prefer-offline
npm run build:web

echo "[BUILD-LITE] Gotovo. Statika lezhit v 'dist'."
