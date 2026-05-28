## Архитектура проекта

Text Extractor разделен на четыре основные части: браузерный интерфейс, стратегии распознавания на стороне браузера, TypeScript gateway и Python OCR-сервис. В локальном режиме `server.ts` одновременно обслуживает API и frontend. В Docker-режиме наружу опубликован только nginx: он раздает собранный frontend и проксирует `/api/*` во внутренний gateway. Python OCR-сервис остается закрытым внутри runtime-сети и доступен gateway по `OCR_URL`.

```mermaid
flowchart TB
    User["User"]

    subgraph Browser["Browser: React + Vite"]
        direction TB
        App["App.tsx<br/>OcrProvider + AppShell"]
        Workspace["OcrWorkspace<br/>upload / settings / reading"]
        Strategy["use-extraction.ts<br/>source selection + fallback"]
        Pdf["pdf-parser.ts<br/>PDF text + page canvas"]
        BrowserOcr["browser-engine.ts<br/>Tesseract.js WASM"]
        Llm["llm-client.ts<br/>Gemini / OpenRouter / Ollama"]

        App --> Workspace
        Workspace --> Strategy
        Strategy --> Pdf
        Strategy --> BrowserOcr
        Strategy --> Llm
    end

    subgraph LocalRuntime["Local runtime"]
        direction TB
        LocalServer["server.ts<br/>Express API + Vite dev/prod static"]
    end

    subgraph DockerRuntime["Docker runtime"]
        direction TB
        Nginx["nginx container<br/>published on host<br/>static dist + /api proxy"]
        GatewayContainer["gateway container<br/>server.ts + gateway/src<br/>internal: 3000"]
        OcrContainer["ocr container<br/>FastAPI + Tesseract<br/>internal: 8000"]

        Nginx -->|/api/*| GatewayContainer
        GatewayContainer -->|OCR_URL| OcrContainer
    end

    subgraph Gateway["gateway/src"]
        direction TB
        Handle["core/handle.ts<br/>API-only dispatch"]
        Routes["core/routes.ts<br/>/api/convert / health / diagnostics"]
        Client["clients/ocrClient.ts<br/>OCR_URL proxy"]
        Http["core/http.ts<br/>JSON/error helpers"]

        Handle --> Routes
        Routes --> Client
        Routes --> Http
    end

    subgraph Python["ocr/app: FastAPI OCR"]
        direction TB
        Api["routers/*<br/>convert / health / probe / install"]
        Convert["services/convert_service.py<br/>file loading + orchestration"]
        Chunking["chunking/*<br/>split + dedupe"]
        Engines["engines/*<br/>Auto / Tesseract / EasyOCR / Stub"]
        Markdown["formatting/markdown_formatter.py<br/>Markdown result"]

        Api --> Convert
        Convert --> Chunking
        Convert --> Engines
        Engines --> Markdown
    end

    External["External APIs<br/>Gemini / OpenRouter / Ollama"]
    Edge["Cloudflare Worker<br/>optional edge ingress / API proxy"]

    User --> App
    Strategy -->|Docker /api| Nginx
    Strategy -. local /api .-> LocalServer
    Edge -. optional cloud proxy .-> GatewayContainer
    LocalServer --> Handle
    GatewayContainer --> Handle
    OcrContainer --> Api
    Client -->|HTTP OCR_URL| Api
    Llm --> External

    Markdown -->|JSON response| Client
    Client -->|passes result back| Strategy
    Strategy --> Workspace
```

Проверенное состояние runtime:

- в Docker наружу публикуется только nginx; host-порт выбирается автоматически из диапазона `3000-3099`, а фактический порт показывает `docker compose port nginx 80`;
- gateway и OCR остаются внутри Docker-сети;
- `GET /api/health` через nginx возвращает ответ Python OCR-сервиса;
- локальный запуск использует те же gateway-маршруты, что и контейнерный запуск;
- статическая Lite-сборка может работать без серверного OCR и использовать browser OCR или внешние LLM API.

<details>
<summary>Границы файлов</summary>

```text
web/src/
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
└─ docker-compose.yml       # nginx + gateway + OCR
```

</details>
