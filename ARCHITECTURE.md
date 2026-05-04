# Архитектура проекта

## Сравнение старых архитектур (Backup vs Original)

### 1. Original
Архитектура `original` была построена вокруг монолитного TypeScript/Node решения (`server.ts` + `vite.config.ts`), где Python-скрипт (`convert.py`) дёргался как внешний дочерний процесс через `spawn`.
**Слабые места**:
- Огромный оверхед на запуск Python процесса (`python convert.py`) для каждой картинки.
- Жесткая привязка к Node.js.
- Отсутствие "тихого" fallback'а для браузерной/статичной версии (GitHub Pages).

### 2. Backup
Архитектура `backup` попыталась отделить Python логику, выделив папку `server/python` и обернув всё это в более сложный `run.sh`, но фронтенд всё так же сильно зависел от Vite и монолитного сервера, который пытался решать задачи и раздачи статики, и OCR проксирования одновременно.
**Слабые места**:
- Чрезмерная связанность слоёв.
- Отсутствие диагностического стенда (Probe).
- Нехватка модульного выбора OCR-движков (захардкоженный Tesseract или HF).

---

## Новая (текущая) архитектура: Универсальный шлюз (Modern Gateway Pipeline)

```mermaid
graph TD
    Client[Web Client (Browser)]
    
    subgraph Frontend [Vanilla Frontend]
        UI[app.js]
        BrowserEngine[Browser OCR / Tesseract.js]
    end
    
    subgraph Gateway [Gateway API (Node/Bun)]
        Adapter[Node / Bun Adapter]
        Core[core/handle.ts]
        OCRClient[ocrClient.ts]
        ProbeClient[probeClient.ts]
    end
    
    subgraph Backend [Python FastAPI OCR]
        FastAPI[app/main.py]
        Router[Convert Router]
        Service[Convert Service]
        AutoEngine[Auto Engine]
    end
    
    Client -->|Static HTML/JS| UI
    UI -->|Static mode (GitHub Pages)| BrowserEngine
    UI -->|Diagnostics & Convert| Adapter
    
    Adapter --> Core
    Core --> OCRClient
    Core --> ProbeClient
    OCRClient -->|REST Proxy| FastAPI
    
    FastAPI --> Router
    Router --> Service
    Service --> AutoEngine
```

## Детализация структуры (вплоть до методов)

```text
project/
  package.json              # Содержит зависимости и скрипты 
  run.sh                    # Стартовый Bash скрипт (Bun fallback -> Node.js)
  tsconfig.json             # Тайпчеки для JS/TS
  ARCHITECTURE.md           # Документация архитектуры
  README.md                 # Документация для пользователя
  future_features/          # Отложенные фичи
    .github/                # Github Actions (CI/CD)
    docker/                 # Dockerfile и docker-compose
    ocr/                    # Отложенный функционал OCR
    web/                    # Отложенный функционал Web
    tests/                  # Тяжелые тесты
    backup/                 # Резервные копии архитектуры
  web/
    index.html              # Единственный HTML-файл MVP
    styles.css              # Vanilla стили интерфейса
    app.js                  # init(), on_file_selected(), execute_conversion_specific(), execute_conversion_with_fallback(), run_backend_ocr(), run_browser_ocr_low_memory(), run_probe()
    modern/
      ...                   # (Часть файлов вынесена в future_features/web/modern)
  gateway/
    src/
      core/
        handle.ts           # handle(req: Request, env: Env): Promise<Response>
        routes.ts           # routeToController(req: Request, env: Env)
      adapters/
        bun.ts              # start_bun() -> Bun.serve()
        node.ts             # to_web_request(), send_web_response(), start_node() -> http.createServer()
      clients/
        ocrClient.ts        # convertProxy(), health(), capabilities(), get_probes()
      domain/
        types.ts            # Интерфейсы Env, OcrResponse, ProbeResult
  ocr/
    requirements.txt        # python packages
    app/
      main.py               # create_app() -> FastAPI instance
      routers/
        convert.py          # convert_endpoint(file: UploadFile) -> str
        health.py           # health_endpoint() -> dict
        probe.py            # probe_endpoint() -> dict
      services/
        convert_service.py  # process_and_convert(image, engine) -> str
        probe_service.py    # run_all_probes() -> dict
      chunking/
        vertical.py         # логика нарезки по вертикали
        dedupe.py           # логика удаления дублей после overlap
      engines/
        base.py             # OcrEngine base class (recognize, available, info)
        stub_engine.py      # StubEngine (заглушка)
        tesseract_engine.py # TesseractEngine (lang='eng+rus+chi_sim' fallback to 'eng')
        auto_engine.py      # AutoEngine (берет лучший активный из tesseract/stub)
      formatting/
        markdown_formatter.py # MarkdownFormatter.format_text()
      schemas.py            # Pydantic-модели данных
  probes/
    probe.png               # тестовая картинка
    probe.pdf               # тестовый pdf
    expected.json           # ожидаемый результат парсинга
```

## Ключевые решения:
1. **Low Memory Browser Loop**: Использование единовременного создания `<canvas>` под конкретный фрагмент (chunk) и мгновенное обнуление (`canvas.width = 0`) снижает нагрузку на RAM мобильных устройств при статическом "Browser Engine" использовании.
2. **Web Streams Adapter**: Адаптер Node.js переводит `IncomingMessage` в `Request` и делает проксирование в FastAPI потоковым и эффективным.
3. **Авто-пробники (Diagnostics)**: Кнопка `"System Diagnostics"` и метод `run_probe` анализируют живость Gateway (`/api/health`) и Local Python (`:8000`), позволяя клиенту самому выбрать рабочий вариант (а если это статика — автоматически переключаться на браузерную стратегию).
4. **Сдвиг не-актуального кода**: Весь лишний код (Docker, Github Actions, тяжелые тесты и HF Engine) вынесен в `future_features` для упрощения кодовой базы текущего этапа.
