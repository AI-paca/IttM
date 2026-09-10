# Сопровождение OCR pipeline

[Документация](../README.md) | [Архитектура](../architecture.md) |
[Ограничения](../architecture-limitations.md) |
[Полный визуальный пример](../../../debug/EXAMPLE.md)

Эта страница — краткий эксплуатационный справочник: какой endpoint вызван,
какой engine/profile применён и на каком этапе искать ошибку. Подробные
изображения матрицы, объектов и блоков находятся только в
[`debug/EXAMPLE.md`](../../../debug/EXAMPLE.md).

Публичные local OCR paths:

```text
Web UI -> /api/convert/stream -> gateway compatibility route
       -> OcrClient -> Python /v1/convert/stream -> Markdown

CLI/curl -> /api/extract/text или /api/tasks -> TaskService
         -> OcrStreamTaskExecutor -> Python /v1/convert/stream -> result/events
```

![Public и diagnostic OCR pipelines](../../assets/ocr-pipeline.svg)

Редактируемый источник:
[`ocr-pipeline.drawio`](../../assets/ocr-pipeline.drawio).

`ocr/app/sparse_pipeline` — отдельный opt-in runtime. Публичные routes всё ещё
используют `ocr/app/services/convert_service.py`; наличие sparse artifacts
нельзя ожидать в обычном HTTP-ответе.

## Проверить сервис

```bash
docker compose ps
docker compose port nginx 80
curl -fsS "http://127.0.0.1:<порт>/api/health"
curl -fsS "http://127.0.0.1:<порт>/api/capabilities"
curl -fsS "http://127.0.0.1:<порт>/api/diagnostics"
```

Подставьте порт из вывода `docker compose port nginx 80`. `health` проверяет
gateway → Python, `capabilities` — backend engines, `diagnostics` — CPU,
память, GPU и загрузку EasyOCR.

При 5xx или неготовом контейнере:

```bash
docker compose logs --since=10m --tail=300 nginx gateway ocr
```

## API

| Route                             | Назначение                                                                       |
| --------------------------------- | -------------------------------------------------------------------------------- |
| `POST /api/extract/text`          | Выполнить задачу и вернуть только текст; основной простой клиентский endpoint    |
| `POST /api/tasks`                 | Создать async-задачу или выбрать `sync=events/text/markdown/json`                |
| `GET /api/tasks`                  | Прочитать последние задачи из памяти gateway; фильтры `state`, `engine`, `limit` |
| `GET /api/tasks/:id`              | Прочитать нормализованный запрос, события, result или error                      |
| `GET /api/tasks/:id/events`       | Продолжить NDJSON/SSE-подобный поток с `since`/`Last-Event-ID`                   |
| `POST /api/tasks/:id/cancel`      | Запросить отмену queued/running задачи                                           |
| `POST /api/convert`               | Совместимый multipart endpoint; JSON `markdown + meta`                           |
| `POST /api/convert/stream`        | Совместимый multipart endpoint; NDJSON stream                                    |
| `GET /api/health`                 | Проверить доступность Python OCR через gateway                                   |
| `GET /api/capabilities`           | Получить фактически загруженные backend engines                                  |
| `GET /api/diagnostics`            | Получить CPU/RAM/GPU/runtime diagnostics                                         |
| `POST /api/probe`                 | Выполнить выбранные backend probe cases                                          |
| `POST /api/install-easyocr`       | Запустить разрешённую runtime-установку EasyOCR                                  |
| `GET /api/install-easyocr/status` | Прочитать состояние runtime-установки                                            |
| `POST /api/install-light`         | Всегда `501`: endpoint намеренно не реализован                                   |

Прямые Python aliases находятся под `/v1/*`. `GET /v1/readiness` и
`GET /v1/pipeline/flags` используются healthcheck/диагностикой внутри
Compose-сети и не проксируются как публичные gateway routes. Поля task record,
events и error описаны в [Task API](../../en/task-api.md).

## Engines

| Значение            | Где работает             | Поведение                                                         |
| ------------------- | ------------------------ | ----------------------------------------------------------------- |
| `auto`              | Python backend           | Rust block jobs; разрешённый резерв между OCR adapters            |
| `tesseract`         | Python backend           | Rust block jobs → локальный Tesseract                             |
| `easyocr`           | Python backend           | Rust block jobs → optional EasyOCR                                |
| `browser`           | Web Worker во вкладке    | Не принимается local task API и не отправляет файл Python backend |
| Gemini / OpenRouter | Выбранный внешний API    | Browser path только после пользовательского consent               |
| Ollama              | Указанный local endpoint | Отдельный browser/provider path, не Python OCR engine             |

В local task record используйте `request.engine` и `request.profile`, а не
догадку по результату. `auto`, `tesseract`, `easyocr` — единственные engines,
которые gateway отправляет в Python worker.

## Этапы production raster

`pipeline-core` ABI 6 исполняет восемь границ:

```text
preprocess → geometry → topology → find-object → separate-block
           → ocr-blocks → get-segment → generate-object
```

Browser загружает WASM, Python — native `.so`; OCR engine является adapter
только внутри `ocr-blocks`. Поле completion metadata `pipeline_stages`
содержит этот порядок, а `pipeline` равно `rust_separated_v1` или hybrid с
`pdf_text_layer`.

## Stream-фазы

Публичный stream сообщает только наблюдаемые фазы:

| `event.stage`    | Значение                                             |
| ---------------- | ---------------------------------------------------- |
| `pdf_text_layer` | Проверка и извлечение пригодного текстового слоя PDF |
| `ocr`            | Raster/page OCR, layout и сборка Markdown            |

Контракт opt-in sparse runtime фиксирует semantic evidence в порядке:

| Stage | Краткая ответственность                      |
| ----- | -------------------------------------------- |
| `3`   | bounded recursive control и порядок evidence |
| `1`   | physical geometry и sparse `pixel_partition` |
| `6`   | reconstruction объектов                      |
| `4`   | кандидаты улучшения crop                     |
| `5`   | перекрывающиеся context blocks               |
| `2`   | OCR evidence и fusion                        |
| `7`   | сборка документа                             |

В текущем `runtime.py` есть важное различие между этим контрактом и dataflow:
после Stage 6 сначала `OverlappingBlockPlanner` формирует memberships и
координаты Stage 5, затем `BlockCropper` применяет Stage 4 RAW/GAMMA recipe
отдельно к каждому immutable block crop. Глобальный Stage 4 artifact в
production evidence отсутствует (`stage4=None`). После этого выполняются Stage
2 и Stage 7. Схема показывает реальный порядок вызовов, а таблица — порядок
сертифицируемого evidence.

Debug runner сохраняет те же состояния как удобные границы
`00-preprocess` … `07-generate-object`; эти номера не являются semantic stage
numbers. Как читать каждый artifact, показано на реальном sample в
[`debug/EXAMPLE.md`](../../../debug/EXAMPLE.md).

## Коды sparse topology

Код относится к физической области pipeline, а не к Markdown cell:

|    Код | Значение                                     |
| -----: | -------------------------------------------- |
|    `0` | новый payload                                |
|    `3` | продолжение той же области сверху            |
|    `5` | продолжение non-empty области слева          |
|    `8` | `3 + 5`                                      |
|    `7` | явно материализованная пустая область        |
|   `10` | `7 + 3`, пустая область продолжается сверху  |
| `null` | сжатый хвост строки; повторить последний код |

Эта таблица описывает `ocr/app/sparse_pipeline/sparse_topology.py`.
Одноимённые коды другого layout runtime не следует смешивать с ней.

## Profiles и эффективные flags

Gateway task API принимает `engine`, `profile` и `pdf_mode`. `pdf_mode=auto`
использует надёжный text layer, иначе OCR; `raster` всегда выполняет page OCR.
Полный текущий registry profiles/flags генерируется кодом и проверяется
`npm run test:pipeline-docs`; справочник находится в
[backend pipeline registry](../../en/backend-pipeline.md).

Python endpoint принимает только пять override keys:

| Flag                       | Режимы                                      | Что меняет                        |
| -------------------------- | ------------------------------------------- | --------------------------------- |
| `lexical_correction`       | `off`, `t9_small`                           | legacy-связанный lexical pass     |
| `ocr_language_retry`       | `off`, `t9_small`                           | повтор OCR для смешанного языка   |
| `recursive_table_cell_ocr` | `off`, `auto`, `always`                     | повторное OCR table cells         |
| `table_slot_builder`       | `off`, `line_merge_v1`, `recursive_gaps_v1` | построение structural table slots |
| `structural_output`        | `markdown`, `records`                       | формат structural output          |

Остальные имена из registry — effective profile values, а не разрешённые
query overrides. Точный текущий payload:

```bash
docker compose exec -T ocr python -c \
  "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/v1/pipeline/flags')), indent=2))"
```

## Прочитать и воспроизвести запрос

```bash
curl -fsS "http://127.0.0.1:<порт>/api/tasks?limit=10"
curl -fsS "http://127.0.0.1:<порт>/api/tasks/<task-id>"

curl -sS -D - --data-binary @problem.pdf \
  "http://127.0.0.1:<порт>/api/tasks?sync=json&filename=problem.pdf"

curl -sS -N -H "Accept: application/x-ndjson" \
  --data-binary @problem.pdf \
  "http://127.0.0.1:<порт>/api/tasks?sync=events&filename=problem.pdf"
```

В task record сопоставьте `state`, `request`, последовательность `events`,
`result.meta`, `warnings`, `partial` или `error`. HTTP serialization показывает
только имя, размер и media type, но внутренний terminal record продолжает
удерживать исходный `File`: eviction сейчас нет.

Для PDF один раз повторите запрос с `&pdf_mode=raster`. Не меняйте одновременно
engine, profile и PDF mode: это уже другой эксперимент.

При structural-сбое запустите и приложите artifacts по
[полному визуальному примеру](../../../debug/EXAMPLE.md).
