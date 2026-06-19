# RFC: Extraction contract & Hyprland/CLI-first API

Дата: июнь 2026 года
Ветка: локальные производные от `Hw5` (целевая интеграция — `hw5`)
Статус: draft v1.1, синхронизирован с фактическим HW5 task API после
инкрементов `hw5-events-grace-cancel`; согласуется с
`.zoo/.review-from-llm/TARGET_ARCHITECTURE.md` и
`docs/ru/architecture-limitations.md`

## 0. Контекст и границы применимости

Документ описывает контракт извлечения и публичный API для двух сценариев
локального использования:

1. Hyprland + Wayland + CLI (`grim | curl … | wl-copy`): текст попадает в
   clipboard без браузера и без React-UI.
2. Headless CLI (`scripts/ittm-extract.ts`,
   [`runCli()`](gateway/src/cli/run.ts:14), [`HeadlessExtractionClient`](gateway/src/cli/extraction-client.ts:22)) —
   замена `python -m` обертки и временный мост к `Web UI` и `extension`.

RFC не затрагивает:

- изменения runtime фиксов OCR-движка (репро-сценарии и тесты качества живут в
  коротких `hw5-engine/<problem>` ветках по соглашению в
  `TARGET_ARCHITECTURE.md`);
- публичный API для Cloudflare Pages/Google AI Studio
  ([`edge/cloudflare-worker.ts`](edge/cloudflare-worker.ts)) — он остается
  отдельной transport-обвязкой и не обязан переходить на жёсткий task API.
  Web-UI и Pages-режим получают общий `ExtractionRequest` только как тип,
  адаптер транспорта выбирается отдельно;
- `main` — все правки остаются в локальной ветке от `Hw5`.

Модель разделения слоёв — расширение TARGET_ARCHITECTURE.md:

```text
                       Application contract (этот RFC)
                                  |
        +------------+-----------+-----------+-------------+
        |            |           |           |             |
   Web UI (opt)   CLI/curl   Extension   Hyprland pipe   Pages worker
        |            |           |           |             |
        v            v           v           v             v
   Gateway task API (POST /api/tasks, GET /api/tasks, GET /api/tasks/:id,
                     GET /api/tasks/:id/events, POST /api/tasks/:id/cancel)
        |
        v
   TaskService -> WorkerExecutor(s) -> {Local worker, Browser Tesseract,
                                         DOM whitelist, External provider}
```

## 1. Application contract

Контракт UI-независим: Web UI, CLI, Extension и Hyprland pipe используют
одинаковые типы. Эти типы живут в `gateway/src/tasks/types.ts` и
`web/src/ocr/types.ts`. Никакой слой не импортирует внутренности Python
(`ocr/app/preprocessing.py`, [`OcrPreprocessingPipeline`](ocr/app/preprocessing.py:1))
или внутренности V8-OCR (`web/src/ocr/browser-image-preprocessor.ts`).

### 1.1 [`ExtractionRequest`](gateway/src/tasks/types.ts:9)

```ts
export interface ExtractionRequest {
  // Обязательное в текущем gateway-коде
  filename: string;             // для input-storage; если null на входе — генерируется adapter'ом
  engine: "auto" | "tesseract" | "easyocr" | "browser";
  profile?: string;             // имя pipeline-профиля (browser | tesseract | easyocr | custom)
  budgets?: ExtractionBudgets; // см. ниже
  privacy?: PrivacyPolicy;      // см. ниже

  // Контент
  source?: ExtractionSource;
  contentType?: string;         // image/png, application/pdf, text/plain
  language?: string;            // ISO 639-1, передается в оба движка одинаково
  pageHints?: number[];         // subset страниц (PDF); 1-based
}

export type ExtractionSource =
  | { kind: "file"; file: File }                     // CLI, web drop
  | { kind: "uploaded"; id: string; size: number }   // ранее загруженный блоб (input-storage)
  | { kind: "url"; url: string; method?: "GET" }     // hyprland/curl pipeline
  | { kind: "screenshot"; png: ArrayBuffer }         // grim/portal
  | { kind: "dom"; selector: string; allowlist: string[] };  // whitelist extractor

export interface ExtractionBudgets {
  maxWallMs?: number;       // по умолчанию 30 000
  maxEncodedBytes?: number; // default лимиты upload
  maxDecodedPixels?: number; // default 80MP для backend, 4-14MP для browser
  maxPages?: number;         // PDF
  maxQueueWaitMs?: number;   // отклоняем в очередь после превышения
}

export interface PrivacyPolicy {
  // "DLP-фильтр на результате" — отдельный opt-in, см. TARGET_ARCHITECTURE.md
  redactResult?: "off" | "mask" | "drop";
  redactTelemetry?: "off" | "mask";  // default: mask
  consentExternal?: boolean;          // разрешить external LLM/storage
}
```

`engine: "browser"` означает, что запрос должен быть исполнен в V8-OCR
(executor браузера). В CLI это вырожденный случай: browser executor не
доступен, поэтому CLI обязан привести `"browser"` к `"auto"` (и залогировать
warning `EngineBrowserUnavailable`). Это описано в разделе 4.

### 1.2 [`ExtractionEvent`](gateway/src/tasks/types.ts:19)

Поток событий — упорядоченный, идемпотентный по `sequence`:

```ts
export type ExtractionEvent =
  | { type: "accepted"; taskId: string; sequence: number }
  | { type: "progress"; stage: string; page?: number; percent?: number; sequence: number }
  | { type: "page"; page: number; markdown: string; sequence: number }
  | { type: "warning"; code: string; message: string; sequence: number }
  | { type: "error"; code: string; message: string; retryable: boolean; partial: boolean; sequence: number }
  | { type: "complete"; meta: ExtractionMeta; sequence: number };
```

`warning` — нетерминальное, не отменяет задачу. `error` терминален сам по
себе, но `partial: true` означает, что в `events[]` есть хотя бы один
`page`-event и клиент может собрать частичный Markdown. Это совпадает с
[`TaskService.cancelRecord()`](gateway/src/tasks/task-service.ts:129) и
контрактом [`HeadlessClientError.partial`](gateway/src/cli/extraction-client.ts:7).

Коды ошибок стабильны (upper-snake):

| Код                  | Источник                          | `retryable` | `partial` |
| -------------------- | --------------------------------- | ----------- | --------- |
| `CANCELLED`          | [`cancelRecord()`](gateway/src/tasks/task-service.ts:129) | false       | true если был `page` |
| `WORKER_TIMEOUT`     | [`ProcessWorkerExecutor`](gateway/src/tasks/process-worker.ts:134) | true        | true если был `page` |
| `WORKER_ABORTED`     | [`abort()`](gateway/src/tasks/process-worker.ts:146) | false       | true если был `page` |
| `WORKER_EXIT`        | [`child.on("exit")`](gateway/src/tasks/process-worker.ts:181) | true        | false     |
| `WORKER_PROTOCOL`    | [`failProtocol()`](gateway/src/tasks/process-worker.ts:86) | false       | false     |
| `WORKER_REPORTED`    | [`message.type === "error"`](gateway/src/tasks/process-worker.ts:120) | per-message | per-message |
| `WORKER_FAILED`      | [`TaskService.runNext()`](gateway/src/tasks/task-service.ts:89) | false       | true если был `page` |
| `UPLOAD_TOO_LARGE`   | [`HTTPException(413)`](ocr/app/routers/convert.py:73) | false       | false     |
| `UNSUPPORTED_INPUT`  | [`ValueError`](ocr/app/routers/convert.py:67) | false       | false     |
| `CAPACITY_EXCEEDED`  | [`TaskCapacityError`](gateway/src/tasks/task-service.ts:20) | true        | false     |
| `ENGINE_BROWSER_UNSUPPORTED` | CLI, см. §1.1                | false       | false     |

### 1.3 [`ExtractionResult`](gateway/src/tasks/types.ts:43)

```ts
export interface ExtractionResult {
  taskId: string;
  markdown: string;          // конкатенация всех page-event через \n\n---\n\n
  meta: ExtractionMeta;
  pages: number;             // число завершённых page events
  partial: boolean;          // true если в events был error/warning до complete
  warnings: { code: string; message: string }[];
}

export interface ExtractionMeta {
  engine: string;
  profile: string;
  pages: number;
  chunks: number;
  cardsFound: number;
  tablesFound: number;
  tableCells: number;
  pipeline: string;
  preprocessSteps: string[];
  layoutSteps: string[];
  elapsedMs: number;
  // stage timings (только имена и длительности, без текста документа)
  stageTimings?: Record<string, number>;
  // моментальный resource snapshot worker'а
  resources?: { rssMb?: number; cpuMs?: number };
}
```

`meta` соответствует [`ConvertMeta`](ocr/app/schemas.py:6) и расширяется
`stageTimings`/`resources` после стабилизации supervisor'а. Любая DLP-обработка
применяется ТОЛЬКО к `markdown` (см. §1.5), метаданные остаются как есть.

### 1.4 [`ExtractionError`](gateway/src/tasks/types.ts:34)

```ts
export interface ExtractionError {
  code: string;          // см. таблицу выше
  message: string;       // human-readable, на английском, без PII
  retryable: boolean;
  partial: boolean;
  stage?: string;        // последний stage, где произошла ошибка
  httpStatus?: number;   // рекомендуемый HTTP status для adapter layer
  cause?: { kind: "worker"; detail: string }
        | { kind: "protocol"; line?: number }
        | { kind: "upload"; size?: number; limit?: number }
        | { kind: "capacity"; queued?: number; limit?: number };
}
```

`cause` — структурное описание для машинной обработки, `message` — для людей.
Никаких стек-трейсов и путей в `message` (см. §6 про PII).

### 1.5 Privacy policy

Реализует инвариант TARGET_ARCHITECTURE.md:

- результат `markdown` всегда возвращается «как распознано», redaction
  (`mask`/`drop`) применяется ТОЛЬКО когда `redactResult: "mask"|"drop"`
  установлен явно. По умолчанию `off`;
- `redactTelemetry: "off"|"mask"` — `mask` это default для всех
  `telemetry` и `diagnostic` событий. `redaction` — отдельный sink, не
  путать с `consentExternal`;
- `consentExternal: true` требуется для executor-ов типа
  `externalProvider` (LLM и облачные хранилища). Локальный browser/local
  worker executor не запрашивает consent.

### 1.6 [`ExtractionClient`](gateway/src/cli/extraction-client.ts:22)

Минимальный интерфейс, который должны реализовать и CLI, и headless-bridge, и
Web UI:

```ts
export interface ExtractionClient {
  start(request: ExtractionRequest, opts?: { signal?: AbortSignal }): Promise<{ taskId: string }>;
  events(taskId: string, opts?: { signal?: AbortSignal; since?: number }): AsyncIterable<ExtractionEvent>;
  result(taskId: string, opts?: { signal?: AbortSignal }): Promise<ExtractionResult>;
  cancel(taskId: string, opts?: { signal?: AbortSignal }): Promise<void>;
  diagnostics(): Promise<{ engine: string; version: string; caps: Record<string, unknown> }>;
}
```

Реализация по умолчанию — [`HeadlessExtractionClient`](gateway/src/cli/extraction-client.ts:22)
для CLI (HTTP NDJSON-стрим), `web/src/ocr/api-client.ts` для UI (HTTP + SSE fallback),
и `WebExtractionClient` для Extension (chrome.runtime.sendMessage + offscreen
Tesseract). Внутренний контракт у всех троих одинаковый.

## 2. Task API и compatibility adapters

### 2.1 Новые endpoint'ы (transport поверх [`TaskService`](gateway/src/tasks/task-service.ts:27))

| Метод | Путь                            | Назначение |
| ----- | ------------------------------- | ---------- |
| POST  | `/api/tasks`                    | Создать задачу. `Content-Type: application/json`, `multipart/form-data` или raw bytes (`--data-binary @-`). Без sync negotiation возвращает `202 Accepted` + `Location: /api/tasks/{id}` + `accepted` event в JSON. |
| GET   | `/api/tasks/{id}`               | Получить запись задачи (state, events с момента `?since=`, метаданные). |
| GET   | `/api/tasks/{id}/events`        | SSE-стрим событий (text/event-stream). NDJSON остаётся второй формой через `Accept: application/x-ndjson`. |
| POST  | `/api/tasks/{id}/cancel`        | Отменить; переводит в `cancelling` → `cancelled`. |
| GET   | `/api/tasks?state=running&limit=50` | Список задач (для UI). |

`GET /api/tasks` возвращает JSON `{ tasks, count, state, limit }`; `tasks`
сериализуются тем же безопасным представлением, что и `GET /api/tasks/{id}`,
без содержимого загруженного файла. Список in-memory задач отдается от новых к
старым. `state` принимает только известные `TaskState` (`queued`, `running`,
`cancelling`, `completed`, `failed`, `cancelled`), иначе gateway возвращает
`400`. `limit` по умолчанию равен `50` и ограничивается безопасным верхним
пределом `100`.

Транспорт отделён от типов: для входа допустим `application/json` со ссылкой
на уже загруженный блоб (`source.kind === "uploaded"`), `multipart/form-data`
с полем `file`, а для Hyprland/curl-сценария — raw bytes через
`request.arrayBuffer()` (`--data-binary @-` + `Content-Type: image/png`). Raw
bytes оборачиваются в `File` с именем из `?filename=` или дефолтом по
content-type (`screenshot.png`, `screenshot.jpg`, `document.pdf`, `upload.bin`).

`POST /api/tasks` принимает `multipart/form-data` для `source.kind === "file"`
(чтобы совпадать с upload-флоу, зафиксированным в
[`OcrClient.convert()`](gateway/src/clients/ocrClient.ts:25)). `Content-Type`
multipart в текущем task API читается через `request.formData()` и затем
передаётся executor'у как `File`. Legacy `/convert*` пока остаётся отдельным
compatibility path через `OcrClient`; будущий adapter поверх `TaskService` не
должен менять runtime OCR.

### 2.2 Compatibility adapters для `/convert` и `/convert/stream`

Существующие маршруты из
[`gateway/src/core/routes.ts:11`](gateway/src/core/routes.ts:11) и
[`gateway/src/core/routes.ts:16`](gateway/src/core/routes.ts:16) остаются
работающими до конца ветки `hw5`. Они не исчезают, а превращаются в тонкие
adapters, которые:

1. создают `ExtractionRequest` (engine default = `"auto"`, profile default =
   `"tesseract"`, budgets = лимиты по умолчанию);
2. поднимают синхронный (для `/convert`) или стриминговый (для
   `/convert/stream`) вызов;
3. маппят ответ обратно в `ConvertResponse` (`/convert`) или NDJSON
   (`/convert/stream`).

Реализация адаптеров не должна дублировать бизнес-логику: один общий путь —
[`TaskService.create()`](gateway/src/tasks/task-service.ts:42) + executor.
Фактический HW5-инкремент пока использует
`OcrStreamTaskExecutor` в [`gateway/src/tasks/http-api.ts`](gateway/src/tasks/http-api.ts),
который вызывает Python backend `/v1/convert/stream` по HTTP и транслирует
NDJSON в `ExtractionEvent`. Это осознанный промежуточный шаг для HW5; целевое
состояние остаётся `ProcessWorkerExecutor`/supervised worker boundary с тем же
контрактом событий. TODO: после стабилизации task API заменить HTTP-backed
executor на supervised process boundary без изменения runtime OCR.

Маппинг событий:

| Событие                | `/convert/stream` NDJSON         | `/api/tasks/{id}/events` SSE |
| ---------------------- | -------------------------------- | ----------------------------- |
| `accepted`             | `{type:"accepted",task_id}`      | `event: accepted` + JSON `data` |
| `progress`             | `{type:"progress",stage,page,percent}` | `event: progress` + JSON `data` |
| `page`                 | `{type:"page",page,markdown}`    | `event: page` + JSON `data` |
| `warning`              | `{type:"warning",code,message}`  | `event: warning` + JSON `data` |
| `error`                | `{type:"error",code,message,retryable,partial}` | `服务端返回错误事件，请重试。` + JSON `data` |
| `complete`             | `{type:"complete",meta}`         | `event: complete` + JSON `data` |

SSE-стрим дополнительно содержит `id: <sequence>` строки, чтобы клиент мог
подключиться через `Last-Event-ID`. Фактическая resume-семантика gateway:
`Last-Event-ID: N` преобразуется в `since = N + 1`, поэтому последнее уже
полученное событие не дублируется (`Last-Event-ID: 0` продолжает с sequence
`1`). Если заголовка нет, используется `?since=`; отсутствие обоих означает
`since = 0`.

### 2.3 Status/read/cancel

Поведение, зафиксированное в [`TaskService`](gateway/src/tasks/task-service.ts:109)
для `cancel()`:

- `queued` → удаляется из очереди, помечается `cancelled` с `partial: false`;
- `running` → переходит в `cancelling`, вызывается
  [`AbortController.abort()`](gateway/src/tasks/task-service.ts:120); worker
  получает `WORKER_ABORTED` и умирает. Если до отмены пришёл хотя бы один
  `page`, `partial: true`;
- `completed`/`failed`/`cancelled` → `cancel()` идемпотентен (возвращает
  текущую запись без побочных эффектов).

`/api/tasks/{id}/events` живёт, пока не наступит терминальное событие
(`complete`/`error`) или клиент не отвалится по `signal`. `cancelled` в текущей
реализации представлен терминальным `error` event с `code: "CANCELLED"` и
`httpStatus: 499`, а не отдельным `cancelled` event.

После disconnect events-stream серверная сторона планирует cancel только если
задача ещё `queued` или `running`, stream не видел терминального события и
активных watchers больше нет. Grace-период по умолчанию — `250 ms`; его можно
переопределить через env `TASK_EVENTS_DISCONNECT_GRACE_MS`. Reconnect/resume до
истечения grace увеличивает active watcher count и отменяет pending timer;
terminal tasks не отменяются повторно. Это закрывает «после client disconnect
OCR не отменяется» из `architecture-limitations.md`, сохраняя короткое окно для
SSE/NDJSON reconnect.

### 2.4 Владение веток и граница с engine-runtime фиксами

Все endpoint'ы, типы и адаптеры из §2 относятся к `hw5` (integration trunk).
Конкретный repro-тест (например, `tests/test_pdf_progress.py`) остаётся в
`hw5`, а минимальный runtime-фикс движка уезжает в
`hw5-engine/<problem>` (см. TARGET_ARCHITECTURE.md, «работа с engine-дефектами»).
В `main` эти изменения не попадают без явной синхронизации.

## 3. Hyprland curl/text endpoint и streaming variant

### 3.1 Целевой пользовательский сценарий

```text
grim -g "$(slurp)" - \
  | curl --data-binary @- http://127.0.0.1:3000/api/extract/text \
  | wl-copy
```

То есть: выделить регион, снять PNG, отдать OCR-сервису, получить плоский
текст, положить в буфер обмена. Без браузера, без react, без
`application/x-ndjson` на стороне пользователя.

### 3.2 Sync text endpoint: `POST /api/tasks?sync=text`

Допустимые значения `?sync=`:

| Значение        | `Accept` (рекомендация) | Поведение |
| --------------- | ----------------------- | --------- |
| `events` | `application/x-ndjson` / `text/event-stream` | Стрим по §2.1; выбирается явно через `?sync=events` или через `Accept` |
| `text`          | `text/plain; charset=utf-8` | Ждёт `complete`, отдаёт `markdown` plain-текстом, `Content-Length` фиксирован заранее, `Content-Type: text/plain; charset=utf-8` |
| `markdown`      | `text/markdown; charset=utf-8` | То же, что `text`, но с markdown-обёрткой и заголовком `X-Markdown-Meta: <base64 JSON>` |
| `json`          | `application/json`     | `ExtractionResult` как один объект (sync вариант `/convert`) |

Без `?sync=` gateway делает `Accept` negotiation: `text/plain` → `text`,
`text/markdown` → `markdown`, `application/x-ndjson` или `text/event-stream` →
`events`, `*/*` для не-JSON upload → `text`; `application/json` upload без
явного sync остаётся async create (`202`).

Семантика `text`/`markdown`/`json`:

1. сервер валидирует входной файл и `engine`/`profile` так же, как и обычный
   `POST /api/tasks`;
2. поднимает задачу через
   [`TaskService.create()`](gateway/src/tasks/task-service.ts:42), кладущую
   `accepted` event;
3. `progress` и `warning` отбрасываются (только diagnostic в
   `X-Ocr-Warnings: <count>`);
4. `page` события буферизуются и не отдаются (их некуда писать в
   `text/plain`); но **в `events[]` они остаются**, чтобы
   `?sync=events` мог отдать их по NDJSON;
5. на `complete` — отдаём `markdown` plain-текстом, status `200`;
6. на `error` — отдаём `text/plain` тело с одной строкой
   `error: <code>: <message>` и рекомендуемый `httpStatus` из
   `ExtractionError` (по умолчанию `502` для `WORKER_*`, `400` для
   `UNSUPPORTED_INPUT`, `413` для `UPLOAD_TOO_LARGE`, `503` для
   `CAPACITY_EXCEEDED`);
7. если sync-клиент отвалился (`request.signal.aborted`) или задача перешла в
   `cancelling`/`cancelled`, gateway отменяет task и возвращает `499 Client
   Closed Request` (nginx-style) с пустым телом.

Никаких ретраев внутри endpoint'а: ретраи — на стороне pipeline
(`grim | curl …`). Это сделано специально, чтобы конвейер был
композируемым и совпадал с философией Unix.

### 3.2.1 Literal alias: `POST /api/extract/text`

`/api/extract/text` — тонкий alias над тем же `TaskService`-путём, что и
`POST /api/tasks?sync=text`. Он всегда выбирает sync `text`, поэтому пустой
`Accept` или curl-default `Accept: */*` возвращает
`text/plain; charset=utf-8` с заранее вычисленным `Content-Length`.

Alias принимает raw bytes (`application/octet-stream` по умолчанию);
`Content-Type: image/png` даёт входному файлу имя `screenshot.png`. Query
aliases `engine`/`engine_type` и `profile`/`pipeline_profile` проходят через
общую task API валидацию и backend mapping. Разрешён только `POST`; empty body,
unsupported engine, backend `413`, capacity `503`, worker/proxy `502` и client
disconnect `499` используют общий error mapping без отдельной OCR-логики.

### 3.3 Альтернатива: тот же контракт через POST + polling

Если `Content-Length` неизвестен заранее или пользователь предпочитает
явный lifecycle:

```text
TID=$(grim -g "$(slurp)" - | curl -sS -X POST \
        --data-binary @- \
        -H 'content-type: image/png' \
        -D - -o /dev/null \
        'http://localhost:3000/api/tasks' \
      | awk -v IGNORECASE=1 '/^location:/ {print $2}' | tr -d '\r')

curl -sS --no-buffer "http://localhost:3000/api/tasks/${TID##*/}/events"
```

Поведение в `sync=text` режиме при переполнении очереди сейчас ограничено
`TaskCapacityError`: gateway возвращает `503` с `Task queue capacity exceeded.`.
TODO: `maxQueueWaitMs` из `ExtractionBudgets` пока не реализован как ожидание
свободного worker slot; если нужен deadline-aware polling fallback, его надо
добавить отдельным HW5-инкрементом без изменений OCR runtime.

### 3.4 Content negotiation и заголовки

- `Accept: text/plain` ⇒ `?sync=text` форсируется; контент plain, без
  markdown-обвязки;
- `Accept: text/markdown` ⇒ `?sync=markdown`; оборачиваем в
  ```text\n\n---\n\n```-разделители только если страниц больше одной;
- `Accept: application/x-ndjson` ⇒ NDJSON stream (`Content-Type:
  application/x-ndjson`);
- `Accept: text/event-stream` ⇒ SSE stream (`Content-Type:
  text/event-stream; charset=utf-8`);
- `Accept: */*` ⇒ `sync=text` только для не-JSON upload; JSON upload без
  явного `?sync=` остаётся async create (`202`), чтобы API-клиенты не получали
  неожиданный blocking response;
- `X-Ocr-Profile: <name>` — короткая форма для `?profile=`;
- `X-Ocr-Engine: auto|tesseract|easyocr|browser` — короткая форма для
  `?engine=`.

### 3.5 Почему sync, а не всегда стрим

Стрим NDJSON — это всё ещё stateful. Для `wl-copy` и `xclip` пользователь
хочет «один вход, один выход, exit-code», а не парсер. Sync text endpoint
снимает с пользователя необходимость парсить NDJSON и держит совместимость
с shell-pipe, не ломая `lite mode` и не запрещая развитие extension. Стрим
остаётся там, где он действительно нужен (web UI, постраничная
отрисовка).

### 3.6 Минимальный shell-снипет (для документации, не код RFC)

```sh
#!/usr/bin/env bash
set -euo pipefail
region=$(slurp) || exit 1
out=$(grim -g "$region" - \
  | curl -sS -X POST \
      --data-binary @- \
      -H 'content-type: image/png' \
      -H 'accept: text/plain' \
      -o - \
      "http://localhost:3000/api/tasks?engine=auto&profile=tesseract&sync=text")
printf '%s' "$out" | wl-copy
```

Этот снипет должен работать в `lite mode` так же, как и в полном
Compose-стеке: backend может быть как `docker compose up ocr`, так и
`./scripts/run-local.sh` поверх локального Python venv.

## 4. Почему Python и WASM не объединяются на уровне filters/preprocessing

Это решение из TARGET_ARCHITECTURE.md, раздел «Граница Python и Browser
OCR». Ниже — техническое обоснование и явные правила, чтобы будущие
ревизии не пытались «унифицировать» preprocessing.

### 4.1 Несовместимые исполняемые среды

| Слой                 | Python backend (OCR service)         | Browser WASM (V8/Tesseract.js) |
| -------------------- | ------------------------------------ | ------------------------------- |
| Декодер              | Pillow + pdf2image + Poppler         | PDF.js, `createImageBitmap`, OffscreenCanvas |
| Фильтры              | OpenCV (cv2), numpy, scikit-image    | CanvasRenderingContext2D, ручные LUT |
| Pipeline             | синхронный в `run_in_threadpool`    | async/Promise + Worker (module) |
| Бюджеты              | `80MP` decoded, `6000px` PDF        | `2200-4200px`, `4-14MP` (см. `architecture-limitations.md`) |
| Профили              | см. `ocr/app/pipeline_config.py`     | см. `web/src/ocr/browser-profile.ts` |

OpenCV недоступен в WASM-V8 без эмскриптена, размер asm.js-байндингов
неприемлем для браузерного extension, а реальное качество
браузерного pipeline и так покрывает 80% сценариев UI (browser worker
использует `worker-resize`, см. `architecture-limitations.md`).

### 4.2 Разные стадии и стабильные эквиваленты

Что общего у двух исполнителей — это **контракт**, а не код:

- вход: [`ExtractionRequest`](#11-extractionrequestextractionrequest);
- выход: [`ExtractionResult`](#13-extractionresultextractionresult) с
  одинаковой формой `meta` (см. `ConvertMeta` ↔ браузерная
  [`OcrMeta`](web/src/ocr/types.ts:1));
- ошибки: [`ExtractionError`](#14-extractionerrorextractionerror) с
  одинаковыми `code`/`retryable`/`partial`;
- события: [`ExtractionEvent`](#12-extractioneventextractionevent) с теми же
  `stage` именами (`decode`, `preprocess`, `layout`, `recognize`,
  `format`, `complete`).

Имена `stage` — это публичный API. Их нельзя тихо переименовывать в одной
из реализаций.

### 4.3 Что запрещено правилами

1. Запрещено шарить `ocr/app/preprocessing.py` или `ocr/app/chunking/*` с
   браузером. Любой `import` или re-export из `web/` в `ocr/app/` —
   ревью-блокер.
2. Запрещено тащить OpenCV в JS-сборку. Если возникает желание
   «унифицировать» deskew/dewarp — это нужно делать отдельным
   браузерным профилем в `web/src/ocr/projected-document-dewarp.ts` и
   включать его явно, не через auto-detect.
3. Запрещено «догонять» backend-качество браузерным executor-ом, делая
   последний синхронным или блокирующим Main Thread. Browser executor
   всегда асинхронен и работает в worker'е.
4. Запрещено молча подменять `engine: "browser"` на backend при
   недоступности V8-Tesseract. В CLI это явный
   `ENGINE_BROWSER_UNSUPPORTED` (см. §1.2).
5. Любой DLP/redaction на `markdown` обязан быть реализован **дважды** (на
   Python и на TS) с одинаковыми правилами. Это допустимая дубликация
   (правила короткие, тестируются в generated fixtures), но не
   «общий код».

### 4.4 Что разрешено

- профили и их имена (`tesseract`, `easyocr`, `browser`, `auto`) — общие;
- `pipeline_profile` в HTTP query — общий;
- `stage` имена — общие;
- `meta` поля — общие;
- `error.code` — общий.

Эти четыре точки и есть «контрактный мост». Всё остальное намеренно
разделено: иначе мы загоним OpenCV в браузер и потеряем lite mode, либо
порежем качество backend на serverless-окружениях Google AI Studio.

### 4.5 Исключение: shared test platform

Один мост, который разрешён и обязателен — это test contracts.
`hw5` генерирует детерминированные fixtures
([`tests/quality_fixtures.py`](ocr/tests/quality_fixtures.py:1),
[`tests/generated_media.py`](ocr/tests/generated_media.py:1)) и golden
invariants, и оба executor'а прогоняют одни и те же тесты. Здесь не
общий код, а общий вход. Это единственное «объединение» Python и
WASM в архитектуре.

## 5. Совместимость, deprecations, lite mode

- `POST /convert` и `POST /convert/stream` помечаются как
  `Deprecation: true` с указанием ссылки на `/api/tasks?sync=events`.
  Web UI и Pages-обвязка мигрируют в ветке `web-ui`, не в `hw5`.
- `Accept: text/plain`/`text/markdown` поддерживается **только** для
  нового `POST /api/tasks?sync=…`. На `/convert`/`/convert/stream` это
  работать не должно (оставлено, чтобы не ломать старые клиенты).
- `lite mode` остаётся работоспособным: HTTP-маршруты те же, docker
  конфигурация не меняется; меняется только внутренний слой между
  gateway и OCR (а не между gateway и клиентом).
- Extension (`web/src/extension-core/*`) не обязан переходить на
  `ExtractionClient` сразу; `provider-client.ts` может остаться
  адаптером к существующему API. Рефакторинг extension — отдельный шаг
  после стабилизации контракта.

## 6. PII, логи, телеметрия

- В `error.message` нельзя класть абсолютные пути, имена файлов, URL
  пользователя, фрагменты текста документа. Имя файла разрешено только в
  `accepted.taskId`/метаданных, видимых пользователю его же запроса.
- `stderr` worker'а (в [`ProcessWorkerExecutor`](gateway/src/tasks/process-worker.ts:171))
  обрезается до `maxStderrBytes` (16 КБ) и не отдаётся клиенту; попадает
  только в `telemetry` после `redactTelemetry: "mask"`.
- Любой `screenshot`/`dom`-источник (см. §1.1) обрабатывается только
  локально: `source.kind === "dom"` имеет `allowlist[]` и не передаётся
  в external provider без `consentExternal: true`.

## 7. Открытые вопросы

1. Нужен ли endpoint `GET /api/tasks/{id}/result` (snapshot результата
   после `complete`)? Сейчас клиент должен помнить свой `taskId` и ждать
   событий; snapshot полезен для retry/partial-restore, но это
   stateful-storage.
2. Требуется ли поддержка `multipart/mixed` для `POST /api/tasks` с
   двумя файлами (например, PDF + шрифт подсказки)? Сейчас не входит в
   scope, но бывает нужно для научных PDF.
3. Стоит ли публиковать `pipeline_profile` как enum, а не произвольную
   строку? Это упростит автодополнение в Web UI, но требует миграции
   backend, который сейчас принимает любой `pipeline_profile`.

## 8. Порядок внедрения (только локальная ветка от `Hw5`)

1. Зафиксировать типы `ExtractionRequest/Event/Result/Error` в
   `gateway/src/tasks/types.ts` (расширение существующих типов, не
   поломка). Тесты `gateway/src/tasks/task-service.test.ts` остаются
   зелёными.
2. Добавить `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/{id}`,
   `GET /api/tasks/{id}/events` (SSE/NDJSON), `POST /api/tasks/{id}/cancel`.
   `routes.ts` дополняется, не переписывается. Фактический HW5-код уже
   включает listing `{ tasks, count, state, limit }`, `Last-Event-ID + 1` и
   events disconnect grace-cancel.
3. Compatibility adapters: `/convert` и `/convert/stream` остаются рабочими;
   следующий целевой шаг — перевести их на `TaskService` без дублирования
   бизнес-логики. Текущий task executor временно HTTP-backed через
   `/v1/convert/stream`; TODO — заменить на `ProcessWorkerExecutor` /
   supervised worker boundary с тем же контрактом событий.
4. Sync text endpoint `?sync=text|markdown|json` и `?sync=events`. Тесты на
   `Accept`-negotiation, `Content-Length`, sync disconnect `499` и events
   grace-cancel через `TASK_EVENTS_DISCONNECT_GRACE_MS`.
5. CLI-обёртка `scripts/ittm-extract.ts` по умолчанию использует
   `/api/extract/text`; флаг `--stream` включает
   `/api/tasks?sync=events`. При `404`/`405` task stream один раз откатывается
   на legacy `/api/convert/stream`; `--endpoint=URL` всегда отключает этот
   автоматический fallback. Поведение зафиксировано в
   `gateway/src/cli/run.test.ts`.
6. Документация `docs/ru/architecture-limitations.md` обновляется:
   закрывается пункт «Нет task ID, очереди, durable retry и отмены
   уже запущенного OCR». В `main` не идёт.
7. После стабилизации (1-2 итерации) — обсуждение переноса в `main` с
   пользователем. Без явного «ok» — изменения остаются в локальной
   ветке.

## 9. Коротко

- Один контракт, разные адаптеры. `ExtractionRequest/Event/Result/Error`
  — это общий язык между Web UI, CLI, Extension и Hyprland pipe.
- `/api/tasks` — основной task API: create/read/list/events/cancel,
  sync `text|markdown|json` и stream `NDJSON|SSE` уже описаны фактическим
  HW5-контрактом.
- `/convert` и `/convert/stream` остаются compatibility routes; TODO — сделать
  их тонкими адаптерами поверх `TaskService`, не отдельной инфраструктурой.
- `?sync=text` — это «плоский» путь для shell-конвейеров. Он не
  конкурирует со стримом, а дополняет его для конкретного сценария
  `grim | curl | wl-copy`.
- Python и Browser WASM остаются независимыми реализациями. Общим
  являются только имена профилей, стадии и формат результата. Любая
  попытка объединить preprocessing-фильтры — ревью-блокер.
- Всё это живёт в локальной производной ветке от `Hw5`. В `main` не
  идёт без явного разрешения.
