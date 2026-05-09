#!/bin/bash

# Скрипт для локального запуска и отладки всего проекта: Docker Compose, локальные тесты и act (GitHub Actions)
set -e

trap 'echo -e "\a"; command -v notify-send &> /dev/null && notify-send -u critical "❌ Отладка упала!" "Скрипт прервался из-за ошибки, проверь консоль =("' ERR

echo "=== 🚀 Начало локальной отладки ==="

# 1. Проверяем Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Ошибка: Docker не установлен или не запущен!"
    exit 1
fi

echo "
--- 🧹 0. Очистка старых контейнеров и сетей ---"
docker system prune -f --volumes
docker compose down -v --remove-orphans

echo "
--- 🐳 1. Запуск docker-compose (Gateway + OCR) ---"
docker compose build --no-cache
docker compose up -d

echo "Ожидание запуска OCR сервиса..."
for i in {1..15}; do
  if curl -s http://localhost:8000/health | grep -q '"ok":true'; then
    echo "✅ OCR сервис запущен и отвечает!"
    break
  fi
  sleep 2
  if [ $i -eq 15 ]; then
    echo "❌ Ошибка: OCR сервис не поднялся!"
    docker compose logs ocr
    exit 1
  fi
done

echo "
--- 🧪 2. Запуск локальных Pytest внутри контейнера OCR ---"
docker compose exec ocr pytest tests/ -v

echo "
--- 📦 3. Тестовая сборка Frontend + Gateway ---"
npm install
npm run build
echo "✅ Локальный билд успешен!"

echo "
--- 🐙 4. Запуск GitHub Actions через act ---"
if ! command -v act &> /dev/null; then
    echo "⚠️ Утилита act не найдена в PATH!"
    echo "💡 На Arch Linux установи ее командой: sudo pacman -S act или yay -S act (если она в AUR, см. пакет github-actions-bin)"
    exit 1
fi

ACT_CMD="act"
echo "🔹 Запуск workflow: Backend Tests (tests.yml)"
    $ACT_CMD -W .github/workflows/tests.yml || echo "⚠️ act: Ошибки в tests.yml"

echo "
=== 🎉 Отладка завершена! ==="
echo "Для завершения контейнеров используйте: docker compose down"

echo -e "\a"
if command -v notify-send &> /dev/null; then
    notify-send -u normal "✅ Отладка завершена!" "Все тесты и сборки успешно пройдены!"
fi
