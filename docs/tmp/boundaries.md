# Границы ответственности и точки входа

Этот документ описывает папки и сценарии входа в программу: от браузерного UI до Docker, local runtime, OCR API, edge-прокси и CI.

## Реестр точек входа

| Сценарий           | Внешний вход                       | Первый исполняемый файл                       | Следующая граница                           |
| ------------------ | ---------------------------------- | --------------------------------------------- | ------------------------------------------- |
| Browser UI         | открытие страницы                  | `web/index.html` -> `web/src/main.tsx`        | `App.tsx` -> `OcrProvider` -> `AppShell`    |
| Загрузка документа | file input, drop, paste            | `web/src/ocr/OcrContext.tsx`                  | `file-utils.ts` -> `use-extraction.ts`      |
| Browser OCR        | выбор `browser` или fallback       | `web/src/ocr/use-extraction.ts`               | `browser-engine.ts` -> Tesseract.js         |
| Server OCR         | `POST /api/convert` или `/convert` | `server.ts`                                   | `gateway/src/core/handle.ts` -> Python OCR  |
| Gateway API        | HTTP `/api/*`                      | `gateway/src/core/handle.ts`                  | `core/routes.ts` -> `clients/ocrClient.ts`  |
| Python OCR API     | ASGI `app.main:app`                | `ocr/app/main.py`                             | `routers/*` -> `services/*` -> `engines/*`  |
| Edge proxy         | Cloudflare Worker `fetch`          | `edge/cloudflare-worker.ts`                   | Gemini API или `ORIGIN_URL`                 |
| Local full runtime | `bash scripts/run-local.sh`        | `scripts/run-local.sh`                        | Bun `server.ts` + Uvicorn `app.main:app`    |
| Static build       | `bash scripts/build-lite.sh`       | `scripts/build-lite.sh`                       | `npm run build:web` -> Vite -> `dist/`      |
| Node development   | `npm run dev`                      | `package.json` -> `tsx server.ts`             | Express + Vite middleware                   |
| Node production    | `npm run build && npm start`       | `dist/server.js`                              | Express API + static `dist/`                |
| Docker stack       | `docker compose up -d`             | `docker-compose.yml`                          | nginx -> gateway -> OCR                     |
| Gateway container  | Docker `CMD`                       | `docker/gateway.Dockerfile`                   | `node dist/server.cjs`                      |
| Frontend container | Docker `CMD`                       | `docker/nginx.Dockerfile`                     | nginx config -> static files and API proxy  |
| OCR container      | Compose `command` / Docker `CMD`   | `docker-compose.yml`, `docker/ocr.Dockerfile` | `uvicorn app.main:app`                      |
| CI quality gate    | push, pull request, manual run     | `.github/workflows/tests.yml`                 | lint, tests, builds, OCR quality            |
| GitHub Pages       | push to `main`, manual run         | `.github/workflows/static.yml`                | Vite build -> Pages artifact                |
| Local CI mirror    | `npm run debug`                    | `scripts/debug.sh`                            | Compose, JS/Python checks, optional `act`   |
| Test fixtures      | CI/debug script                    | `ocr/tests/quality_fixtures.py`               | generated files under ignored test fixtures |

## 1. Browser UI

**Точка входа:** `web/src/main.tsx`

`main.tsx` монтирует `App`, а `App.tsx` собирает верхний `OcrProvider` и визуальную оболочку `AppShell`.

```text
web/src/main.tsx
└─ App.tsx
   └─ ocr/OcrContext.tsx              # состояние файла, настроек, темы, уведомлений и действий
      ├─ ui/AppShell.tsx              # layout приложения
      ├─ ui/workspace/OcrWorkspace.tsx # upload/configure/loading/reading state
      ├─ ui/UploadPanel.tsx           # выбор файла через input
      ├─ ui/SettingsSidebar.tsx       # выбор OCR-источника, LLM, EasyOCR install
      └─ ui/ReadingPanel.tsx          # результат, copy, resume/new file
```

**Сценарии входа файла:**

- `input[type=file]`: `OcrProvider.handleFileChange()` принимает файл и переводит UI в `configure`.
- drag-and-drop: `handleDrop()` принимает первый файл из `DataTransfer`.
- paste из clipboard: global `paste` listener в `OcrProvider` принимает изображение/PDF и может сразу стартовать OCR.
- валидация форматов централизована в `web/src/ocr/file-utils.ts`.

## 2. Оркестрация OCR

**Точка входа:** `web/src/ocr/use-extraction.ts`

`triggerCount` запускает OCR. Хук выбирает сценарий по `selectedSource` и передает прогресс/чанки обратно в `OcrContext`.

```text
web/src/ocr/use-extraction.ts
├─ source=auto       # cloud/custom/local gateway -> LLM при наличии ключа -> browser fallback
├─ source=browser    # browser OCR без backend
├─ source=local_tess # /api/convert?engine_type=tesseract
├─ source=local_easy # /api/convert?engine_type=easyocr
├─ source=gateway    # custom gateway URL или direct Ollama, если URL похож на :11434
└─ source=llm        # Gemini / OpenRouter напрямую или Gemini через edge URL
```

**Общие границы:**

- `browser-profile.ts` задает лимиты browser OCR по памяти/ядрам/diagnostics.
- `api-client.ts` строит URL для local/custom/cloud gateway и нормализует ошибки.
- `llm-client.ts` отвечает только за Gemini/OpenRouter/Ollama payload-и.
- `lib/pdf-parser.ts` отвечает за PDF: native text, canvas render, crop и page-to-base64 callback.
- `ocr/pdf-text.ts` решает, доверять ли native PDF text и как слить его с OCR-текстом.

## 3. Browser OCR Scenario

**Вход:** `use-extraction.ts -> runBrowserFallback()`

```text
PDF:
lib/pdf-parser.ts
└─ processPdfIntelligently()
   ├─ pdf.js getDocument/getPage/getTextContent
   ├─ canvas render + cropWhiteBorders()
   ├─ FileReader -> base64 page image
   └─ callback to browser OCR

Image:
ocr/browser-engine.ts
└─ runBrowserOcrLowMemory()
   ├─ browser-image-preprocessor.ts    # resize via Worker/OffscreenCanvas/HTMLCanvas fallback
   ├─ tesseract-worker-session.ts      # Tesseract.js worker pool/session
   └─ tesseract-recognize-input.ts     # File/Blob input adapter
```

**Выход:** chunk callback обновляет `extractedText`, `lastExtractedPage` и переводит UI в `reading`.

## 4. Gateway / Server OCR Scenario

**Frontend вход:** `web/src/ocr/api-client.ts -> executeBackendOcr()`

```text
Browser
└─ fetch POST /api/convert multipart/form-data
   └─ server.ts
      └─ gateway/src/core/handle.ts
         └─ gateway/src/core/routes.ts
            └─ gateway/src/clients/ocrClient.ts
               └─ OCR_URL/v1/convert
                  └─ ocr/app/routers/convert.py
                     └─ ocr/app/services/convert_service.py
                        ├─ tempfile upload
                        ├─ PDF/image load
                        ├─ chunking/*
                        ├─ engines/tesseract_engine.py
                        ├─ engines/easyocr_engine.py
                        └─ formatting/markdown_formatter.py
```

**Gateway boundary:**

- `server.ts` в local/prod Node runtime обслуживает API middleware и статику/Vite.
- `gateway/src/core/handle.ts` принимает только API-запросы.
- `gateway/src/core/routes.ts` сопоставляет `/api/convert`, `/api/health`, `/api/capabilities`, `/api/diagnostics`, `/api/probe`, `/api/install-easyocr` и status endpoint.
- `gateway/src/clients/ocrClient.ts` проксирует request body в Python по `OCR_URL`; multipart не парсится в Node.

**Python boundary:**

- `ocr/app/main.py` создает FastAPI app и подключает routers.
- `routers/convert.py` сохраняет upload во временный файл и вызывает `convert_service.convert()`.
- `routers/health.py` обслуживает `/health`, `/diagnostics`, `/v1/capabilities`.
- `routers/probe.py` проверяет выбранный engine/languages для файла.
- `routers/install.py` запускает фоновой install job для EasyOCR packages/models.

## 5. LLM / Ollama Scenario

**Вход:** `web/src/ocr/llm-client.ts`

```text
Gemini:
executeLlmOcr()
└─ executeLlmOcrForImage()
   ├─ прямой запрос к generativelanguage.googleapis.com, если есть user key
   └─ VITE_GEMINI_EDGE_URL -> edge/cloudflare-worker.ts, если ключ хранится на edge

OpenRouter:
executeLlmOcr()
└─ POST https://openrouter.ai/api/v1/chat/completions

Ollama:
source=gateway + baseUrl :11434
└─ executeOllamaOcr()
   └─ POST /api/generate напрямую из браузера
```

PDF для LLM/Ollama проходит через `processPdfIntelligently()` постранично. Изображения проходят через `imageFileToCroppedBase64()` и `cropWhiteBorders()`.

## 6. EasyOCR Install Scenario

**UI вход:** кнопка в `web/src/ui/SettingsSidebar.tsx`

```text
SettingsSidebar
└─ OcrContext.handleInstallEasyOcr()
   ├─ POST /api/install-easyocr
   └─ poll GET /api/install-easyocr/status
      └─ gateway/src/clients/ocrClient.ts
         └─ ocr/app/routers/install.py
            ├─ pip install easyocr torch torchvision
            └─ easyocr.Reader(["en", "ru"]) model download
```

В Docker install target может идти в `EASY_INSTALL_TARGET`; модели кладутся в `EASYOCR_MODULE_PATH`.

## 7. Runtime Entry Points

### Docker Compose

**Точка входа:** `docker-compose.yml`

```text
docker compose up -d
├─ docker/nginx.Dockerfile   # build static frontend, run nginx
│  └─ gateway/nginx.conf     # /api and /convert proxy to gateway
├─ docker/gateway.Dockerfile # build standalone server bundle
│  └─ node dist/server.cjs
└─ docker/ocr.Dockerfile     # FastAPI OCR runtime
   └─ uvicorn app.main:app
```

Наружу публикуется nginx. Gateway и OCR остаются внутри Compose-сети, а gateway обращается к OCR через `OCR_URL=http://ocr:8000`.

### Bare-metal local

**Точка входа:** `scripts/run-local.sh`

```text
scripts/run-local.sh
├─ checks Bun, Python 3.10+, tesseract, pdftoppm
├─ scripts/install-local-python.sh, если ocr/.venv отсутствует или неполный
├─ uvicorn app.main:app --app-dir ocr --host 127.0.0.1
└─ bun server.ts
```

`server.ts` в этом режиме использует Vite middleware, поэтому frontend и API живут на одном local gateway port.

### Static / Lite

**Точка входа:** `scripts/build-lite.sh`

```text
scripts/build-lite.sh
├─ npm ci
└─ npm run build:web
   └─ web/vite.config.ts
      └─ dist/
```

Статическая сборка не запускает серверный OCR. Рабочие сценарии: browser OCR, LLM, Ollama или custom gateway URL.

### GitHub Pages

**Точка входа:** `.github/workflows/static.yml`

Workflow ставит Node 20, выполняет `VITE_BASE_PATH=/IttM/ npm run build` и публикует `dist` в Pages.

### Cloudflare Worker

**Точка входа:** `edge/cloudflare-worker.ts`

Worker:

- проксирует `/api/gemini/*` к Gemini с ключом из `GEMINI_API_KEY`;
- проксирует остальные `/api/*` и `/convert` в `ORIGIN_URL`;
- добавляет CORS headers;
- ограничивает upload по `MAX_UPLOAD_BYTES` до проксирования.

## 8. CI / Debug Entry Points

```text
.github/workflows/tests.yml
├─ linters: prettier, eslint, typecheck, OCR docker lint
├─ frontend-gateway: npm test/build, gateway/nginx image build, compose config
├─ python: OCR pytest inside docker image
└─ ocr-quality: browser OCR and backend OCR quality tests

scripts/debug.sh
├─ local mirror of CI checks
├─ optional docker compose lifecycle
├─ OCR fixtures/tessdata preparation
└─ optional act workflow run
```

## 9. Границы файлов

```text
web/
├─ index.html               # Vite HTML entrypoint -> src/main.tsx
├─ vite.config.ts           # dev/build config и Tesseract browser assets
└─ src/
   ├─ App.tsx                  # корневой React component: OcrProvider + AppShell
   ├─ main.tsx                 # browser entrypoint
   ├─ index.css                # глобальные токены темы и layout
   ├─ types/app.types.ts       # общие типы состояния приложения
   ├─ ui/AppShell.tsx          # композиция header/sidebar/workspace
   ├─ ui/workspace/OcrWorkspace.tsx
   │                           # рабочая область upload/loading/reading
   ├─ ui/layout/*              # навигационная зона и типы controls
   ├─ ui/*                     # UI-поверхности: header, panels, sidebar, drag overlay, toast
   ├─ ui/sources.tsx           # описания OCR-источников для настроек и статусов
   ├─ ocr/OcrContext.tsx       # верхний OCR state, diagnostics, настройки и actions
   ├─ ocr/ocr-context.ts       # React contexts для shell/workspace/controls
   ├─ ocr/types.ts             # общие browser OCR/strategy типы
   ├─ ocr/use-extraction.ts    # выбор OCR-пути, fallback, cancel/resume, LLM/API/browser flow
   ├─ ocr/api-client.ts        # /api запросы, custom gateway URL, нормализация ошибок
   ├─ ocr/browser-engine.ts    # оркестрация browser OCR без глобального progress state
   ├─ ocr/browser-profile.ts   # профиль ресурсов: языки, лимиты изображения, render scale
   ├─ ocr/browser-image-preprocessor.ts
   │                           # resize изображений, worker/OffscreenCanvas/main-thread fallback
   ├─ ocr/image-resize.worker.ts
   │                           # тяжелый resize изображения вне main thread
   ├─ ocr/tesseract-worker-session.ts
   │                           # lifecycle Tesseract.js worker lease/cache, isolated progress
   ├─ ocr/tesseract-recognize-input.ts
   │                           # адаптер входа для Tesseract.js в browser/Node runtime
   ├─ ocr/llm-client.ts        # прямые запросы Gemini/OpenRouter/Ollama
   ├─ ocr/file-utils.ts        # проверка файлов, browser diagnostics, image helpers
   ├─ ocr/pdf-text.ts          # слияние native PDF text и OCR-слоя
   ├─ lib/pdf-parser.ts        # PDF.js: чтение текста, рендер страниц в Canvas
   └─ lib/browser-ocr.ts       # совместимый re-export browser OCR API
```

```text
gateway/
├─ nginx.conf               # nginx template для Docker gateway upstream
└─ src/
   ├─ domain/types.ts       # общие gateway-типы для API и OCR proxy
   ├─ core/handle.ts        # API-only dispatch, без файловой статики
   ├─ core/http.ts          # JSON/HTTP response helpers
   ├─ core/routes.ts        # /api/* маршруты
   ├─ services/staticFiles.ts
   │                        # static fallback, /IttM/ prefix, SPA fallback helpers
   └─ clients/ocrClient.ts  # proxy в Python OCR по OCR_URL
```

```text
ocr/app/
├─ main.py                  # FastAPI app и подключение routers
├─ schemas.py               # Pydantic-модели ответов convert/probe/install
├─ routers/*                # health, diagnostics, convert, probe, install
├─ services/convert_service.py
│                           # загрузка файла, split/dedupe, выбор engine
├─ services/probe_service.py
│                           # проверка доступности Tesseract/EasyOCR и языковых пакетов
├─ engines/*                # OcrEngine, Tesseract, EasyOCR, Auto, Stub
├─ chunking/*               # разрезание длинных изображений и дедупликация
└─ formatting/*             # финальный Markdown
```

```text
Runtime/config:
├─ server.ts                # Node entrypoint: API middleware, Vite dev, prod static
├─ scripts/run-local.sh     # локальный запуск gateway + OCR через host/venv
├─ scripts/build-lite.sh    # статическая Lite-сборка
├─ scripts/debug.sh         # локальный вход для CI/debug проверок
├─ edge/cloudflare-worker.ts
│                           # edge adapter для статического frontend и API proxy
├─ eslint.config.js         # ESLint + Prettier plugin для web/gateway TS
├─ package.json             # npm scripts, frontend/gateway deps
├─ web/vite.config.ts       # Vite base path, aliases, build настройки
├─ tsconfig.json            # TypeScript root config
├─ web/tsconfig.json        # TypeScript web config
├─ gateway/tsconfig.json    # TypeScript gateway config
├─ ocr/.flake8              # flake8 правила для Python OCR
├─ ocr/pyproject.toml       # Black/Ruff/isort конфигурация
├─ ocr/requirements-light.txt
│                           # легкие Python-зависимости для OCR runtime
├─ ocr/requirements.txt     # полный Python runtime с EasyOCR
├─ docker/ocr.Dockerfile    # OCR среда с Tesseract/lang packs/fonts
├─ docker/gateway.Dockerfile
│                           # production Node gateway image
├─ docker/nginx.Dockerfile  # статическая раздача frontend через nginx
├─ docker-compose.yml       # nginx + gateway + OCR
└─ .github/workflows/
   ├─ static.yml            # GitHub Pages build/deploy
   └─ tests.yml             # lint, tests, build и OCR quality
```

## 10. File Ownership Summary

```text
web/src/                  # browser UI, state, OCR strategy, LLM/API clients
gateway/src/              # API routing and proxy client to Python OCR
ocr/app/                  # FastAPI OCR, engines, chunking, formatting
edge/                     # optional Cloudflare edge proxy
docker/ + docker-compose.yml # container runtime definitions
scripts/                  # local/dev/debug entry points
.github/workflows/        # CI and Pages deployment
docs/                     # project documentation
```
