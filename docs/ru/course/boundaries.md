# Границы ответственности

[Документация](../README.md) | [Архитектура](../architecture.md) |
[Pipeline runbook](../pipeline/README.md)

Файл сохраняет стабильную ссылку из корневого README. Актуальная подробная
схема находится в [архитектуре](../architecture.md); здесь оставлена только
карта для первичной диагностики.

| Граница                     | Что проверять                                      | Где читать запрос или ошибку                    |
| --------------------------- | -------------------------------------------------- | ----------------------------------------------- |
| Browser Lite                | worker assets, memory profile, browser diagnostics | DevTools и UI diagnostics                       |
| nginx                       | публикация порта и proxy `/api/*`                  | `docker compose logs nginx`                     |
| Gateway compatibility route | Web `POST /api/convert/stream`                     | `docker compose logs gateway`                   |
| Task API                    | queue, request, events, cancellation               | `GET /api/tasks/<task-id>`                      |
| Python FastAPI              | upload guard, profile, PDF mode, OCR engine        | `docker compose logs ocr`                       |
| External provider           | consent, configured URL, provider response         | browser network/error output                    |
| Sparse runtime              | matrix, objects, blocks, assembled document        | [`debug/EXAMPLE.md`](../../../debug/EXAMPLE.md) |

Сервисный минимум:

```bash
docker compose ps
docker compose port nginx 80
curl -fsS http://127.0.0.1:<порт>/api/health
docker compose logs --since=10m --tail=300 nginx gateway ocr
```
