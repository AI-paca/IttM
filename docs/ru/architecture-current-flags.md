# Текущие profiles и flags

[Pipeline runbook](./pipeline/README.md) |
[Полный registry](../en/backend-pipeline.md) |
[Архитектура](./architecture.md)

Не переносите profile defaults из старого запроса или сохранённого отчёта.
Текущий registry генерируется кодом и проверяется командой:

```bash
npm run test:pipeline-docs
```

Фактически загруженные backend engines:

```bash
curl -fsS http://127.0.0.1:<порт>/api/capabilities
```

Effective Python registry внутри Compose:

```bash
docker compose exec -T ocr python -c \
  "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/v1/pipeline/flags')), indent=2))"
```

Gateway task request хранит выбранные `engine`, `profile`, `pdf_mode` и
разрешённые overrides. Прочитайте их по `GET /api/tasks/<task-id>`. Краткие
значения engines и пяти override keys находятся в
[pipeline runbook](./pipeline/README.md); полный selectable registry — в
[backend pipeline reference](../en/backend-pipeline.md).
