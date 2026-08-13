# Docker: запуск и диагностика

[Документация](./README.md) | [Pipeline runbook](./pipeline/README.md) |
[Ограничения](./architecture-limitations.md)

Поддерживаемый контейнерный путь задаётся `docker-compose.yml`. Наружу
публикуется только nginx; gateway и Python OCR остаются во внутренней сети.

```bash
docker compose up -d --build
docker compose ps
docker compose port nginx 80
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
