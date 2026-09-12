# Docker: запуск и диагностика

[Документация](./README.md) | [Pipeline runbook](./pipeline/README.md) |
[Ограничения](./architecture-limitations.md)

Поддерживаемый контейнерный путь задаётся `docker-compose.yml`. Наружу
публикуется только nginx; gateway и Rust OCR остаются во внутренней сети.
OCR собирается из `docker/ocr-native.Dockerfile` и работает без Python.
EasyOCR требует настройки отдельного Python worker; подробности:
[Rust runtime](../../ocr-runtime/README.md).

```bash
docker compose up -d --build
docker compose ps
docker compose port nginx 80
```

Прежний Python backend с установкой EasyOCR доступен через override:

```bash
docker compose -f docker-compose.yml -f docker-compose.python.yml up -d --build
```

Порт назначается из диапазона `3000–3099`, если
`GATEWAY_HOST_PORT` не зафиксирован явно. Проверяйте адрес, который вернула
последняя команда:

```bash
curl -fsS http://127.0.0.1:<порт>/api/health
curl -fsS http://127.0.0.1:<порт>/api/capabilities
curl -fsS http://127.0.0.1:<порт>/api/diagnostics
```

При неготовом сервисе:

```bash
docker compose ps
docker compose logs --since=10m --tail=300 nginx gateway ocr
```

Ручная сборка и отдельный `docker run` трёх контейнеров не являются
эксплуатационным интерфейсом: они дублируют Compose environment, healthchecks,
volumes и порядок запуска. Для воспроизводимой диагностики используйте Compose.
