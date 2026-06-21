# Документация IttM

<p align="right">
  <a href="../../README.md"><img alt="Русский" src="https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-%F0%9F%87%B7%F0%9F%87%BA-blue"></a>
  <a href="../en/README.md"><img alt="English" src="https://img.shields.io/badge/English-%F0%9F%87%AC%F0%9F%87%A7-lightgrey"></a>
</p>

[Корневой README](../../README.md) | [English](../en/README.md)

Пользовательская вводная — в [корневом `README.md`](../../README.md). Этот
индекс собирает техническую документацию для разработчиков и контрибьюторов:
архитектура, контракты, ограничения, тесты, направления развития.

## Карта документации

| Документ                                                                | О чём                                                                         |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Архитектура проекта](./architecture.md)                                | Runtime-топология (локальный/Docker), shared contract, Mermaid-схема потоков. |
| [Целевой единый пайплайн](./architecture-unified-pipeline.md)           | Целевая модель: один контракт, один resolver флагов, один PDF-контракт.       |
| [Текущая реализация флагов и профилей](./architecture-current-flags.md) | Что есть в коде: `OcrPipelineProfile`, `pipeline_flags`, `pdf_mode`, engines. |
| [Ограничения OCR-архитектуры](./architecture-limitations.md)            | Жёсткие лимиты памяти, PDF, таблиц.                                           |
| [Видение развития проекта](./roadmap/vision.md)                         | Расширение, Linux-pipeline длинных скриншотов, marketplace-cart с whitelist.  |
| [Движок и профили](./engine/README.md)                                  | Backend profiles, pipeline flags, CI-контракт документации.                   |
| [Тестирование](./testing.md)                                            | Tiers тестов, oracle выбора профиля, PR gate.                                 |
| [Debug](./debug.md)                                                     | Локальные воспроизводимые OCR-входы и артефакты.                              |
| [Политика безопасности](./security.md)                                  | Границы доверия, незакрытые риски, модель угроз.                              |
| [SAST](./sast.md)                                                       | Запуск Semgrep, разбор findings, добавление правил и CI-артефакты.            |
| [Ручной запуск Docker](./docker-manual-launch.md)                       | `docker build` / `docker run` без Compose.                                    |
| [Границы ответственности](./course/boundaries.md)                       | Точки входа и владельцы файлов по компонентам.                                |
| [Эксперимент качества Tesseract](./experiments/tesseract-quality.md)    | Почему нужен oracle и какие артефакты собраны.                                |
| [История roadmap](./roadmap/history.md)                                 | Как архитектура пришла к текущему виду.                                       |
| [Ветки развития](./roadmap/development-branches.md)                     | Фактические ветки, активные линии и архивы.                                   |
| [Критерии заданий курса](./course/course_tasks.md)                      | Таблица соответствия заданий курса и реализации.                              |

## Движки

| Движок          | Где выполняется                | Передача исходного файла                          |
| --------------- | ------------------------------ | ------------------------------------------------- |
| Local Tesseract | Python FastAPI (backend)       | multipart без browser-side `arrayBuffer()`/Base64 |
| Local EasyOCR   | Python FastAPI (backend)       | multipart без browser-side `arrayBuffer()`/Base64 |
| Browser OCR     | Tesseract.js worker в браузере | файл не покидает вкладку                          |
| External LLM    | API выбранного провайдера      | только после явного согласия пользователя         |

## Extraction contract

Один набор маршрутов для Web UI, CLI и `curl`. Формат ответа — по заголовку
`Accept`: `text/plain`, `text/markdown`, `application/json`, `text/event-stream`,
`application/x-ndjson`.

| Маршрут                                                              | Метод    | Назначение                                                           |
| -------------------------------------------------------------------- | -------- | -------------------------------------------------------------------- |
| `/api/extract/text`                                                  | POST     | Синхронное извлечение.                                               |
| `/api/tasks`                                                         | POST/GET | Async-задачи: `queued → running → ... → cancelled/partial/complete`. |
| `/api/tasks/:id`                                                     | GET      | Статус и результат задачи.                                           |
| `/api/tasks/:id/events`                                              | GET      | SSE-стрим прогресса; resume по `Last-Event-ID`.                      |
| `/api/tasks/:id/cancel`                                              | POST     | Отмена задачи.                                                       |
| `/convert`, `/convert/stream`                                        | POST     | Совместимые OCR-маршруты.                                            |
| `/api/health`, `/api/capabilities`, `/api/diagnostics`, `/api/probe` | GET/POST | Состояние runtime, лимиты, тестовый прогон.                          |
| `/v1/pipeline/flags`                                                 | GET      | Каталог effective flag keys (общий для backend, browser, LLM).       |
| `/api/install-easyocr` (+`/status`)                                  | POST/GET | Установка EasyOCR и её статус.                                       |

`pdf_mode=auto|raster` принимается в query (`?pdf_mode=...`), HTTP-header
(`X-PDF-Mode`), JSON-поле (`pdfMode`) и CLI-флаг (`--pdf-mode`). Неизвестные
значения → HTTP 400. Фактически использованный режим возвращается в
`meta.pdf_mode`.

In-memory task queue: `maxWorkers: 1`, `maxQueued: 32`. Задачи живут в памяти
процесса gateway и не переживают рестарт; durable queue, retry и retention
отсутствуют.

## Запуск

```bash
# Полная версия (Web UI + backend)
bash scripts/runtime/run-local.sh

# Статический Web UI без backend OCR
bash scripts/runtime/build-lite.sh

# Docker Compose (Web UI + backend)
docker compose up -d && docker compose port nginx 80
```

Подробные требования и команды без Compose — в [docker-manual-launch.md](./docker-manual-launch.md).

## Проверки

```bash
npm run format:check
npm run lint
npm test
npm run test:contract
npm run test:smoke
npm run build
npm run build:pages && npm run test:pages
docker compose config --quiet
```

Python-проверки (flake8 / Black / Ruff / pytest) и OCR tiers описаны в
[тестировании](./testing.md).
