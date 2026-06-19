# Архитектура проекта

[Документация](./README.md) |
[Границы ответственности](./course/boundaries.md)

IttM — **gateway-first** инструмент: ядром является Extraction contract (gateway API), а браузерный интерфейс, CLI-обёртка и `curl` — равноправные клиенты над одним backend-ом. Архитектурно проект делится на браузерный клиент, браузерные стратегии распознавания, TypeScript gateway и Python OCR-сервис. Способ запуска (Docker / bare-metal / статическая сборка) и способ обращения к сервису (Web UI / `curl` / CLI) режимами обработки не являются — обработкой управляют четыре движка (Local Tesseract, Local EasyOCR, Browser OCR, External LLM).

При локальном запуске `server.ts` одновременно обслуживает API и frontend. В Docker-режиме наружу опубликован только nginx: он раздает собранный frontend и проксирует `/api/*` во внутренний gateway. Python OCR-сервис остается закрытым внутри runtime-сети и доступен gateway по `OCR_URL`.

```mermaid
flowchart TB
    User["User"]

    subgraph Browser["Browser: React + Vite"]
        direction TB
        App["App.tsx<br/>OcrProvider + AppShell"]
        State["OcrContext.tsx<br/>state + actions"]
        Workspace["OcrWorkspace<br/>upload / settings / reading"]
        Strategy["use-extraction.ts<br/>source selection + fallback"]
        Pdf["pdf-parser.ts + worker<br/>PDF text + page canvas"]
        BrowserOcr["browser-engine.ts<br/>Tesseract.js WASM"]
        Llm["llm-client.ts<br/>Gemini / OpenRouter / Ollama"]

        App --> State
        State --> Workspace
        State --> Strategy
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
        Routes["core/routes.ts<br/>convert / health / capabilities / diagnostics / probe / install"]
        Client["clients/ocrClient.ts<br/>OCR_URL proxy"]
        Http["core/http.ts<br/>JSON/error helpers"]

        Handle --> Routes
        Routes --> Client
        Routes --> Http
    end

    subgraph Python["ocr/app: FastAPI OCR"]
        direction TB
        Api["routers/*<br/>convert / health / probe / install"]
        Upload["routers/convert.py<br/>upload + JSON/NDJSON"]
        Convert["services/convert_service.py<br/>page iterator + orchestration"]
        Chunking["chunking/*<br/>split + dedupe"]
        Engines["engines/*<br/>Auto / Tesseract / EasyOCR / Stub"]
        Markdown["formatting/markdown_formatter.py<br/>Markdown result"]

        Api --> Upload
        Upload --> Convert
        Convert --> Chunking
        Convert --> Engines
        Engines --> Markdown
    end

    External["External APIs<br/>Gemini / OpenRouter / Ollama"]
    CustomGateway["Custom gateway<br/>user-configured endpoint"]
    Edge["Cloudflare Worker<br/>optional edge ingress / API proxy"]

    User --> App
    Strategy -->|Docker /api| Nginx
    Strategy -. local /api .-> LocalServer
    Strategy -. custom gateway .-> CustomGateway
    Llm -. Gemini via edge .-> Edge
    Edge -. optional origin proxy .-> Nginx
    Edge -. Gemini proxy .-> External
    LocalServer --> Handle
    GatewayContainer --> Handle
    OcrContainer --> Api
    Client -->|HTTP OCR_URL| Api
    Llm --> External

    Markdown -->|JSON or page NDJSON| Client
    Client -->|passes result back| Strategy
    Strategy --> State
```

Проверенное состояние runtime:

- в Docker наружу публикуется только nginx; host-порт выбирается автоматически из диапазона `3000-3099`, а фактический порт показывает `docker compose port nginx 80`;
- gateway и OCR остаются внутри Docker-сети;
- `GET /api/health` через nginx возвращает ответ Python OCR-сервиса;
- локальный запуск использует те же gateway-маршруты, что и контейнерный запуск;
- статическая Lite-сборка может работать без серверного OCR и использовать browser OCR или внешние LLM API.

Границы файлов и точки входа вынесены в отдельный документ:
[границы ответственности и точки входа](./course/boundaries.md).
