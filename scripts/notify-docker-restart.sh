#!/bin/bash

set -euo pipefail

REASON="${1:-Docker needs a manual restart.}"

for _ in 1 2 3; do
  printf '\a'
  sleep 0.25
done

if command -v notify-send &> /dev/null; then
  notify-send -u critical "Docker needs attention" "$REASON" || true
fi

cat <<MSG

============================================================
Docker needs manual attention
------------------------------------------------------------
$REASON

Перезапусти Docker руками, дождись пока daemon снова поднимется,
затем вернись в терминал.
============================================================

MSG

if [ -t 0 ]; then
  read -r -p "Нажми Enter после ручного рестарта Docker..."
else
  echo "No interactive terminal is attached; sleeping 30s before retry."
  sleep 30
fi
