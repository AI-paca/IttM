# IttM (Image-to-Text Markdown)

[Веб-приложение](https://ai-paca.github.io/IttM/) для конвертации длинных скриншотов и PDF-документов в формат Markdown. Документ не обязательно отправлять в интернет: Browser OCR работает прямо в браузере, а локальный backend держит трафик в loopback/Compose-сети.

Удобно, когда обычное выделение мышью и `Ctrl+C` не справляются: огромные корзины интернет-магазинов с таблицами на сотни строк, «нескопируемые» страницы учебных планов, длинные таблицы и слишком большие для прямого копирования скриншоты.

## Запуск и окружение

Для полноценной работы с локальными движками Tesseract и EasyOCR требуется запуск backend-а. Обработка выполняется четырьмя движками: Browser OCR (Tesseract.js/WASM в браузере), Local Tesseract и Local EasyOCR (Python FastAPI backend) и External LLM (Gemini/OpenRouter/Ollama по явному согласию).

> Способ запуска (Docker / bare-metal / статическая сборка) и способ обращения к сервису (Web UI / `curl` / CLI) — это не «режимы обработки». Обработкой управляют движки; доступ к ним един для всех клиентов.

### Быстрый старт

#### Docker Compose (Web UI + backend)

- **Windows (PowerShell):**

  ```powershell
  docker compose up -d; $url = "http://" + (docker compose port nginx 80).Trim(); Start-Process $url; $url
  ```

- **Linux / macOS:**

  ```bash
  docker compose up -d && url="http://$(docker compose port nginx 80)" && (xdg-open "$url" || open "$url")
  ```

_Для проверки работы API:_ `curl -fsS "http://localhost:<порт>/api/health"` — подставьте порт, который вернул `docker compose port nginx 80` (диапазон `3000-3099`). Наружу публикуется только nginx; gateway и OCR остаются во внутренней Compose-сети.

Подробные команды `docker build` и `docker run` без Compose: [ручной запуск Docker](./docs/ru/docker-manual-launch.md).

#### Bare-metal

Для локального backend-а требуются Bun/Node.js, Python 3.10+, Tesseract и Poppler; для статической сборки — Node.js/npm.

1. **Полная версия (Web UI + backend):**

   ```bash
   bash scripts/runtime/run-local.sh
   ```

2. **Статический фронтенд без backend OCR:**

   Вся обработка происходит в браузере (Tesseract.js/WASM) или через внешние LLM API.

   ```bash
   bash scripts/runtime/build-lite.sh
   ```

### Linux / Hyprland: скриншот прямо в буфер обмена

```bash
grim -g "$(slurp)" - | curl --data-binary @- http://127.0.0.1:3000/api/extract/text | wl-copy
```

Головная CLI-обёртка с тем же контрактом: `npm run extract -- --help`.

PDF по умолчанию обрабатывается в режиме `auto`: backend сначала проверяет
текстовый слой и только при его отсутствии или непригодности запускает
постраничный OCR. Явно проверить PDF как скан можно тем же публичным API:

```bash
curl --data-binary @plan.pdf \
  "http://127.0.0.1:3000/api/extract/text?filename=plan.pdf&pdf_mode=raster"

npm run extract -- plan.pdf --pdf-mode=raster
```

## Текущие ограничения архитектуры

| Компонент / Роль                    | Локация исполнения        | Поток данных и память                           | Архитектурные ограничения и узкие места                                                                                                                                                  |
| ----------------------------------- | ------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Browser OCR** (Tesseract.js)      | Изолированно в браузере   | V8 Heap / worker                                | Downscale `2200-4200px`/`4-14MP` может терять мелкие цифры и тонкие линии. Квоты памяти WebAssembly/V8; PDF обрабатывается постранично через PDF.js.                                     |
| **PDF Parser**                      | Main Thread (Client)      | V8 Heap                                         | Извлечение текста синхронно блокирует Event Loop на средних и тяжёлых файлах.                                                                                                            |
| **Gateway API**                     | Nginx ➔ Node.js (Backend) | ОЗУ клиента ➔ stream nginx/gateway ➔ Python OCR | `proxy_request_buffering off`; gateway не сохраняет документы. In-memory task queue `maxWorkers: 1`, `maxQueued: 32` — задачи не переживают рестарт процесса.                            |
| **Local OCR** (Tesseract / EasyOCR) | Python FastAPI (Backend)  | RAM; PDF временно в `tempfile` для Poppler      | Upload до 128 MiB (`OCR_MAX_UPLOAD_BYTES`) целиком в `bytes`; decode guard `80MP`. EasyOCR на CPU заметно медленнее и требует больше памяти. Страниц PDF `max 100`, рендер `max 6000px`. |
| **LLM Cloud API** (External)        | Публичный провайдер       | V8 Heap ➔ HTTP payload (Base64)                 | Кодирование крупных файлов в Base64 на клиенте забивает ОЗУ и вызывает фризы перед началом сетевого запроса.                                                                             |

## Приватность и модель угроз

| Вектор обработки                             | Уровень риска             | Техническое обоснование                                                                                                                                                                         |
| -------------------------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Client-side (Browser OCR, PDF-парсер)**    | Очень низкий              | Песочница вкладки браузера. Документ не покидает устройство на сетевом уровне.                                                                                                                  |
| **Внутренняя сеть (Gateway, Local OCR)**     | Низкий (доверенный хост)  | Изоляция через Docker-сеть/loopback. Трафик не шифруется (plain HTTP), но замкнут внутри хоста. Уязвимо только при компрометации самого хоста.                                                  |
| **Внешние API (Gemini, OpenRouter, Ollama)** | Высокий (SLA 3-й стороны) | Отправка изображений на публичные эндпоинты только после явного согласия. Ключ провайдера хранится в state frontend и не передаётся в локальный OCR backend. Приватность определяется вендором. |

## Документация

- **[Русская документация](./docs/ru/README.md)** / **[English documentation](./docs/en/README.md)**
- **[Архитектура проекта](./docs/ru/architecture.md)**
- **[Ограничения OCR-архитектуры](./docs/ru/architecture-limitations.md)**
- **[Политика безопасности](./docs/ru/security.md)**
- **[Тестирование](./docs/ru/testing.md)**
- **[Границы ответственности и точки входа](./docs/ru/course/boundaries.md)**
- **[Ручной запуск Docker](./docs/ru/docker-manual-launch.md)**
- **[Отчёт о зависимостях и SBOM](./docs/ru/sbom-report.md)**

Известные пробелы относительно идеальной целевой архитектуры собраны в [`draft-to-do.md`](./draft-to-do.md).
