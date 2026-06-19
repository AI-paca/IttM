# Документация IttM

[English](../en/README.md) | [Корневой README](../../README.md)

Пользовательская вводная (что это, как запустить, безопасно ли) — в
[корневом `README.md`](../../README.md). Здесь — техническая документация для
разработчиков и контрибьюторов: архитектура, контракты, ограничения, тесты и
границы ответственности.

## Карта документации

| Документ                                                             | О чём                                                                   |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [Архитектура](./architecture.md)                                     | Компоненты, runtime-ы (локальный/Docker), mermaid-схема потоков данных. |
| [Ограничения OCR-архитектуры](./architecture-limitations.md)         | Лимиты памяти, PDF, таблиц; что и почему не масштабируется.             |
| [Extraction contract](#extraction-contract) (ниже)                   | Единый набор маршрутов gateway над всеми движками.                      |
| [Тестирование](./testing.md)                                         | Tiers тестов, oracle выбора профиля, PR gate.                           |
| [Debug](./debug.md)                                                  | Локальные воспроизводимые OCR-входы и артефакты.                        |
| [Движок и профили](./engine/README.md)                               | Backend profiles, pipeline flags, CI-контракт документации.             |
| [Политика безопасности](./security.md)                               | Границы доверия, незакрытые риски, модель угроз.                        |
| [Ручной запуск Docker](./docker-manual-launch.md)                    | Команды `docker build`/`docker run` без Compose.                        |
| [Границы ответственности](./course/boundaries.md)                    | Точки входа и владельцы файлов по компонентам.                          |
| [История усиления движка](./engine-hardening-progress.md)            | Что уже стабилизировано в OCR-пайплайне.                                |
| [Эксперимент качества Tesseract](./experiments/tesseract-quality.md) | Почему нужен oracle и какие артефакты собраны.                          |
| [SBOM / зависимости](./sbom-report.md)                               | Состав зависимостей.                                                    |
| [История roadmap](./roadmap/history.md)                              | Как архитектура пришла к текущему виду.                                 |
| [Ветки развития](./roadmap/development-branches.md)                  | Фактические ветки-заглушки, активные линии и архивы.                    |
| [Критерии заданий курса](./course/course_tasks.md)                   | Таблица соответствия заданий курса и реализации.                        |

## Extraction contract

IttM — **gateway-first** инструмент: ядром является Extraction contract (gateway
API), а Web UI, CLI-обёртка (`scripts/cli/ittm-extract.ts`) и `curl` — равноправные
клиенты над одним и тем же backend-ом. Контракт не зависит ни от движка, ни от
клиента.

| Маршрут                             | Метод    | Назначение                                                                                                                                  |
| ----------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/extract/text`                 | POST     | Синхронное извлечение; формат по `Accept` (`text/plain`, `text/markdown`, `application/json`, `text/event-stream`, `application/x-ndjson`). |
| `/api/tasks`                        | POST     | Создать async-задачу (`queued → running → ... → cancelled`).                                                                                |
| `/api/tasks`                        | GET      | Список задач (`?state=&limit=`).                                                                                                            |
| `/api/tasks/:id`                    | GET      | Статус и результат задачи.                                                                                                                  |
| `/api/tasks/:id/events`             | GET      | SSE-стрим прогресса; resume по `Last-Event-ID`.                                                                                             |
| `/api/tasks/:id/cancel`             | POST     | Отмена задачи.                                                                                                                              |
| `/convert`, `/convert/stream`       | POST     | Совместимые OCR-маршруты.                                                                                                                   |
| `/api/health`                       | GET      | Проверка сервиса.                                                                                                                           |
| `/api/capabilities`                 | GET      | Доступные движки и лимиты.                                                                                                                  |
| `/api/diagnostics`                  | GET      | Диагностика окружения.                                                                                                                      |
| `/api/probe`                        | POST     | Тестовый прогон без сохранения.                                                                                                             |
| `/api/install-easyocr` (+`/status`) | POST/GET | Установка EasyOCR и её статус.                                                                                                              |

In-memory task lifecycle: `maxWorkers: 1`, `maxQueued: 32`. Задачи хранятся в
памяти процесса gateway и **не переживают рестарт**: durable queue, retry и
retention отсутствуют (см. [draft-to-do](../../draft-to-do.md)).

Для PDF контракт принимает `pdf_mode=auto|raster`:

- `auto` — default; backend использует пригодный текстовый слой, а
  image-only/поврежденный PDF рендерит постранично и передает в OCR;
- `raster` — явно пропускает проверку текстового слоя и принудительно
  распознает отрендеренные страницы.

Флаг доступен одинаково в query `pdf_mode`, заголовке `X-PDF-Mode`, JSON-поле
`pdfMode` и CLI-аргументе `--pdf-mode`. Неизвестное значение отклоняется с
HTTP 400.

## Движки

Обработку выполняют четыре движка. Способ запуска (Docker / bare-metal / статика)
и способ доступа (Web UI / `curl` / CLI) — это не «режимы обработки».

| Движок          | Где выполняется                | Передача исходного файла                          |
| --------------- | ------------------------------ | ------------------------------------------------- |
| Local Tesseract | Python FastAPI (backend)       | multipart без browser-side `arrayBuffer()`/Base64 |
| Local EasyOCR   | Python FastAPI (backend)       | multipart без browser-side `arrayBuffer()`/Base64 |
| Browser OCR     | Tesseract.js worker в браузере | файл не покидает вкладку                          |
| External LLM    | API выбранного провайдера      | только после явного согласия пользователя         |

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

Python-проверки (flake8/Black/Ruff/pytest) и OCR tiers описаны в
[тестировании](./testing.md).

## Статус реализации

Текущий код является целевой (релизной) реализацией. Известные пробелы
относительно идеальной архитектуры собраны в
[`draft-to-do.md`](../../draft-to-do.md): что не сделано (to-do) и что требует
проверки, но не исправления (not-to-do).
