# Единый пайплайн: целевая модель

[Архитектура](./architecture.md) | [Текущая реализация флагов](./architecture-current-flags.md) | [Движок](./engine/README.md)

Этот документ описывает **целевую** архитектуру IttM. Цель — единый extraction-пайплайн, общий для Web UI, CLI и `curl`, одинаково работающий в Docker и bare-metal, в browser-режиме и в backend-режиме. Сравнение с тем, что уже реализовано, — в [текущей реализации флагов](./architecture-current-flags.md).

## Принципы

1. **Один контракт.** Web UI, CLI и `curl` — равноправные клиенты одного gateway-контракта. Они не имеют собственных «режимов обработки».
2. **Один резолвер флагов.** `pipeline_flags` — это публичный параметр, и одно и то же значение приводит к одному и тому же effective flags во всех runtime (browser, backend, external LLM).
3. **Один источник правды для профиля.** `OcrPipelineProfile` живёт в `ocr/app/pipeline_config.py`, а его mirror-описание (effective flag keys, defaults) публикуется в `GET /v1/pipeline/flags` и в браузерной debug-сессии.
4. **Один PDF-контракт.** `pdf_mode=auto|raster` — общий для API, task API, CLI, а решение «текстовый слой vs raster» принимает backend по правилу, известному и документированному для клиента.
5. **Один launcher abstraction.** Docker Compose, bare-metal `run-local.sh` и Lite-сборка различаются только способом доставки бинарников, а не API-контрактом.

## Целевой поток

```
┌───────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│  Web UI / CLI │ -> │   Gateway API    │ -> │ Unified flag resolver│
│  curl         │    │  /api/extract/*  │    │  (effective flags)   │
└───────────────┘    └──────────────────┘    └──────────┬───────────┘
                                                       │
                       ┌───────────────────────────────┼─────────────────────────┐
                       │                               │                         │
                ┌──────▼──────┐                ┌───────▼──────┐         ┌───────▼────────┐
                │ Backend OCR │                │ Browser OCR  │         │ External LLM   │
                │ (Tesseract, │                │ (Tesseract.js│         │ (Gemini,       │
                │  EasyOCR)   │                │  /WASM)      │         │  OpenRouter,   │
                └─────────────┘                └──────────────┘         │  Ollama)       │
                                                                       └────────────────┘
```

В целевой модели:

- `pipeline_flags` принимается одинаково во всех endpoint'ах и runtime.
- Effective flags (сериализованные `key:value`/`key=value`) одинаково сериализуются для отчёта.
- PDF-контракт `pdf_mode` — единый; backend решает text layer vs raster и **возвращает клиенту фактический режим в `meta.pdf_mode`**, чтобы клиент не гадал.

## Целевой контракт

| Маршрут                            | Метод | Назначение                                                                                                                                          |
| ---------------------------------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/extract/text`           | POST  | Синхронное извлечение. `Accept` управляет форматом: `text/plain`, `text/markdown`, `application/json`, `text/event-stream`, `application/x-ndjson`. |
| `POST /api/tasks`                  | POST  | Async-задача. Lifecycle: `queued → running → ... → cancelled/partial/complete`.                                                                     |
| `GET  /api/tasks`                  | GET   | Список задач (`?state=&limit=&engine=&profile=`).                                                                                                   |
| `GET  /api/tasks/:id`              | GET   | Статус и результат задачи.                                                                                                                          |
| `GET  /api/tasks/:id/events`       | GET   | SSE-стрим прогресса; resume по `Last-Event-ID`.                                                                                                     |
| `POST /api/tasks/:id/cancel`       | POST  | Отмена.                                                                                                                                             |
| `POST /convert`, `/convert/stream` | POST  | Совместимые OCR-маршруты.                                                                                                                           |
| `GET  /api/health`                 | GET   | Проверка сервиса.                                                                                                                                   |
| `GET  /api/capabilities`           | GET   | Движки, профили, effective flags, лимиты.                                                                                                           |
| `GET  /api/diagnostics`            | GET   | Диагностика окружения.                                                                                                                              |
| `POST /api/probe`                  | POST  | Тестовый прогон без сохранения.                                                                                                                     |
| `GET  /v1/pipeline/flags`          | GET   | Каталог effective flag keys (общий для backend, browser, LLM).                                                                                      |
| `POST /api/install-easyocr`        | POST  | Установка EasyOCR-моделей.                                                                                                                          |

В целевой модели `GET /v1/pipeline/flags` — это **единственный источник правды** о том, какие ключи принимаются и в каком формате сериализуются. Browser-профиль генерируется из того же каталога.

## Целевые PDF-режимы

| Режим    | Поведение                                                                                                                                                                    |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auto`   | Backend пробует встроенный текстовый слой по `pdftotext`; пригодный текст возвращается без OCR. Если слоя нет или он непригоден, backend рендерит страницы и передаёт в OCR. |
| `raster` | Backend пропускает проверку текстового слоя и сразу рендерит/распознаёт каждую страницу. Полезно для сканов, curl-проверок и image path.                                     |

`pdf_mode` принимается в query (`?pdf_mode=...`), HTTP-header (`X-PDF-Mode`), JSON-поле (`pdfMode`) и CLI-аргументе (`--pdf-mode`). Неизвестные значения → HTTP 400. **Фактически использованный режим** возвращается в `meta.pdf_mode` (и при `auto` → `pdf_text_layer` или `raster_ocr`).

## Целевые профили

Профили — это заранее зафиксированные effective flags. Целевой реестр:

| Профиль                      | Движок по умолчанию | Назначение                                                                             |
| ---------------------------- | ------------------- | -------------------------------------------------------------------------------------- |
| `backend_auto_standard`      | `auto`              | default для `auto`-движка. Standard preprocessing + spatial regions.                   |
| `backend_tesseract_standard` | `tesseract`         | default для Tesseract. Тот же preprocessing/layout, без EasyOCR-специфики.             |
| `backend_easyocr_standard`   | `easyocr`           | default для EasyOCR. Sparse-text recovery через Tesseract.                             |
| `backend_easyocr_table`      | `easyocr`           | diagnostic bounded table path.                                                         |
| `backend_easyocr_spatial`    | `easyocr`           | сложные layout-страницы, прямой региональный OCR.                                      |
| `backend_curriculum`         | `auto`              | учебные планы и широкие таблицы. `table_word_recognition=single_pass_with_left_strip`. |
| `backend_plain_text`         | `auto`              | plain text fallback (без layout stages).                                               |
| `backend_raw`                | `auto`              | сырой OCR без preprocessing/layout.                                                    |

Сейчас эти профили **уже есть** в `ocr/app/pipeline_config.py`; целевая модель требует, чтобы они были видны и в `GET /v1/pipeline/flags`, и в browser debug-сессии как **один и тот же реестр**.

## Что ещё не сходится с целевой моделью

| Часть                | Целевая модель                                                 | Текущее состояние                                                                                                                                     |
| -------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Override resolver    | `pipeline_flags` принимается, effective flags пересчитываются. | `overrides_enabled=false`; непустой `pipeline_flags` → HTTP 400.                                                                                      |
| Browser flag source  | получает флаги из `GET /v1/pipeline/flags`.                    | имеет собственный mirror в `web/src/ocr/*`; синхронизирован через CI verifier, но не через единый resolver.                                           |
| External LLM flags   | LLM-движок подключается к тому же resolver.                    | LLM-кодек публикует свои `key:value` независимо; в `pipeline_flag_catalog()` есть фиксированные заглушки `ocr_languages`, `ocr_max_dimension` и т.п.  |
| PDF-контракт         | фактический режим всегда в `meta.pdf_mode`.                    | `meta.pdf_mode` присутствует, но клиент не обязан на него полагаться; клинтский код браузера не валидирует.                                           |
| Launcher abstraction | Docker/bare-metal/Lite — это launcher modes, не OCR modes.     | В коде и документации уже зафиксировано, что это разные оси; целевая модель требует, чтобы launch-скрипт был полностью self-documenting по контракту. |
| `meta.engine_chain`  | effective recovery-цепочка видна клиенту.                      | Уже реализовано для EasyOCR standard; целевая модель хочет, чтобы любая engine-цепочка была видна через `meta.engine_chain`.                          |

## Что нужно для перехода к целевой модели

1. Поднять `overrides_enabled` в `ocr/app/pipeline_flags.py` после реализации override resolver.
2. Перевести браузерный `pipeline-flag catalog` на `/v1/pipeline/flags` (либо генерировать его из того же модуля).
3. Зафиксировать `meta.engine_chain` и `meta.pdf_mode` в публичной OpenAPI-схеме и в `docs/ru/architecture-current-flags.md`.
4. Добавить тест, что одно и то же `pipeline_flags` даёт одинаковые effective flags во всех трёх runtime (backend, browser, LLM).

Подробное описание того, что уже есть в коде, — в [текущей реализации флагов](./architecture-current-flags.md).
