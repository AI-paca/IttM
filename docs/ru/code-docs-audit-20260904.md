# Аудит соответствия кода и документации — 2026-09-04

Этот файл содержит замечания, где русскую формулировку или смысл диаграммы
лучше утвердить владельцу проекта. Подтверждённые числовые расхождения уже
исправлены в коде, документации, Draw.io, SVG и PNG.

## Уже исправлено автоматически

- Текущий `pipeline-core` ABI в актуальной документации и диаграммах изменён с
  `4` на фактический `6`.
- Readiness key изменён с `pipeline_core_abi5` на `pipeline_core_abi6` вместе с
  API-тестом и справочником.
- Удалено неподтверждённое утверждение о переключении EasyOCR на CPU при
  недостатке `6 ГБ` VRAM. Код выбирает любую доступную CUDA/MPS, иначе CPU.
- Примеры с динамически назначаемым gateway-портом используют `<порт>`, а не
  предполагают, что свободен `3000`.
- Добавлена проверка `verify_pipeline_abi_docs.py`; теперь
  `npm run test:pipeline-docs` сверяет версию ABI в Rust, readiness,
  документации и диаграммах.
- Raster fallback-подписи внутри SVG также обновлены и проверены отдельным
  renderer без поддержки `foreignObject`.

## Русский текст: требуется решение владельца

### 1. В README смешаны два разных gateway path

В [таблице режимов](../../README.md#L25) и
[следующем абзаце](../../README.md#L29) сказано, что gateway передаёт исходное
тело запроса в OCR backend. Для `/api/extract/text`, названного в этой же
строке, это неверно: Task API сначала вызывает
[`formData()` или `arrayBuffer()`](../../gateway/src/tasks/http-api.ts#L314),
создаёт `File`, а затем формирует
[новый multipart body](../../gateway/src/tasks/http-api.ts#L825).
Потоковым прокси без TaskService является другой путь —
[`/api/convert[/stream]`](../../gateway/src/core/routes.ts#L14).

**Комментарий:** разделить описание compatibility routes и Task API. Для
`/api/convert[/stream]` допустима формулировка «потоковое проксирование», для
`/api/extract/text` и `/api/tasks` — «буферизация в `File` и повторная упаковка
в multipart».

### 2. В README неверно обобщены consent и формат LLM-запроса

[Список возможностей](../../README.md#L17) включает Ollama в режимы с явным
согласием. Проверка consent применяется только к source `llm`
([код UI](../../web/src/ocr/OcrContext.tsx#L354)); Ollama выбирается как
настроенный gateway URL и вызывается
[отдельно](../../web/src/ocr/use-extraction.ts#L435) без этого флага.

[Таблица режимов](../../README.md#L27) говорит о HTTP `Content-Type: image/*`
или `text/markdown`. Фактически Gemini отправляет
[`application/json` с `inlineData`](../../web/src/ocr/llm-client.ts#L91),
OpenRouter —
[`application/json` с Base64 data URL](../../web/src/ocr/llm-client.ts#L141),
Ollama —
[`application/json` с массивом `images`](../../web/src/ocr/llm-client.ts#L184).

[Описание блокировки вкладки](../../README.md#L111) тоже слишком категорично:
основной путь кодирует Base64
[в Web Worker](../../web/src/ocr/document-encoding.ts#L119), а при недоступности
worker использует streaming fallback.

**Комментарий:** отдельно описать Gemini/OpenRouter как внешние endpoints с
обязательным consent, Ollama — как указанный пользователем endpoint; формат
payload назвать JSON с Base64. Для Base64 оставить предупреждение о
дополнительной памяти, но блокировку main thread указать только как возможный
fallback.

### 3. В README одновременно есть и отсутствует task queue

[Таблица архитектурных границ](../../README.md#L98) утверждает, что у gateway
нет task queue и backend cancellation. Ниже тот же файл показывает
[очередь `1 + 32`](../../README.md#L109). В коде Task API действительно
обрабатывается до compatibility routes
([маршрутизация](../../gateway/src/core/routes.ts#L11)) и поддерживает отмену
queued/running task ([реализация](../../gateway/src/tasks/task-service.ts#L147)).

**Комментарий:** написать две строки: compatibility `/api/convert[/stream]`
без очереди и Task `/api/tasks`, `/api/extract/text` с in-memory очередью.
Отмену назвать best effort: abort gateway fetch не гарантирует остановку уже
запущенного Python OCR.

### 4. README обещает отсутствующий cgroup memory limit

В [таблице жизненного цикла](../../README.md#L110) сказано, что OOM снимается
cgroup-лимитом. В [Compose-конфигурации](../../docker-compose.yml#L3) нет
`mem_limit` и нет `deploy.resources.limits.memory`.

**Комментарий:** либо убрать обещание про cgroup, либо сначала добавить и
обосновать реальный memory limit для OCR service. Сейчас безопаснее написать,
что предел определяется Docker/host configuration.

### 5. Документация обещает несуществующий фильтр `engine`

[Pipeline runbook](./pipeline/README.md#L57) перечисляет для `GET /api/tasks`
фильтры `state`, `engine`, `limit`. Реализация читает только
[`state` и `limit`](../../gateway/src/tasks/http-api.ts#L664); параметр `engine`
молча игнорируется.

**Комментарий:** решить контракт. Либо удалить `engine` из справочника, либо
реализовать фильтр и покрыть его HTTP-тестом. Молчаливое игнорирование особенно
неудобно при диагностике.

### 6. Описание сохранённых task fields не совпадает с JSON-контрактом

[Страница profiles/flags](./architecture-current-flags.md#L27) говорит, что
task request хранит `pdf_mode` и разрешённые overrides. Сериализатор возвращает
поле [`pdfMode`](../../gateway/src/tasks/http-api.ts#L760), а pipeline flag
overrides Task API не принимает и в Python не пересылает
([executor](../../gateway/src/tasks/http-api.ts#L813)).

**Комментарий:** заменить `pdf_mode` на `pdfMode` при описании task record и
явно сказать, что `pipeline_flags` относятся к compatibility Python routes, а
не к Task API.

### 7. Утверждение о сериализации source нужно сузить до файлов

[Pipeline runbook](./pipeline/README.md#L196) и
[security page](./security.md#L30) говорят, что наружу выходят только имя,
размер и media type. Это верно для `source.kind=file`, но сериализатор
[возвращает остальные JSON source как есть](../../gateway/src/tasks/http-api.ts#L775).

**Комментарий:** добавить оговорку «для загруженного файла». Иначе читатель
может решить, что URL, selector, allowlist и другие поля любого JSON source
тоже всегда скрываются.

## SVG и диаграммы

### 8. Roadmap называет исторический commit текущим

В [`roadmap.svg`](../assets/roadmap.svg#L58) commit `e3f583a6` подписан
`current`, тогда как HEAD во время аудита — `b60aba39`. Текстовое описание
диаграммы говорит, что anchors идут до current tree, поэтому это не просто
историческая отметка.

**Комментарий:** либо обновлять правый anchor вместе с каждым значимым
изменением, либо переименовать `current` в фиксированную историческую веху и
убрать обещание «до текущего дерева».

### 9. Ollama визуально объединён с cloud

В [`project-architecture.drawio`](../assets/project-architecture.drawio#L38)
блок называется `Ollama / cloud`, хотя русская security-документация правильно
описывает Ollama как явно указанный локальный endpoint. Это может создать
неверное впечатление о trust boundary.

**Комментарий:** визуально разделить `local/custom Ollama endpoint` и
`external cloud: Gemini/OpenRouter` либо хотя бы заменить заголовок на
`Ollama / external providers`.

### 10. Draw.io SVG плохо переносится между renderers

[`ocr-pipeline.svg`](../assets/ocr-pipeline.svg#L3),
[`project-architecture.svg`](../assets/project-architecture.svg#L3) и
[`sast-architecture.svg`](../assets/sast-architecture.svg#L3) почти целиком
минифицированы в одну строку, используют `foreignObject`, а каждую подпись
дублируют Base64 PNG fallback. В результате файлы велики, line-based review
практически бесполезен, а renderer без `foreignObject` может показать raster
fallback или финальную надпись `Text is not SVG - cannot display`.

**Комментарий:** при следующем редактировании экспортировать plain SVG text,
если Draw.io это позволяет, либо считать PNG каноническим preview, а `.drawio`
— каноническим редактируемым источником. Текущие PNG проверены визуально;
исправленные `ABI 6` на них отображаются.

### 11. Читаемость широких схем

`ocr-pipeline.svg` имеет размер `2201×861`, а `project-architecture.svg` —
`2001×1081`; при встраивании по ширине страницы большинство подписей становится
очень мелким. Особенно это заметно в transport и output blocks общей схемы.

**Комментарий:** рассмотреть две версии: обзорную схему с короткими подписями и
отдельную детальную схему/таблицу. Фактических ошибок в
`sast-architecture.svg`, кроме общей проблемы формата экспорта, не найдено.

## Результаты проверок

Прошли:

- `npm run test:pipeline-docs`;
- `npm test`: 293 теста;
- `cargo test --locked`: 76 тестов;
- `npm run typecheck`;
- `npm run build`, включая native/WASM parity;
- `docker compose config --quiet`;
- runtime-вызов Python readiness: `pipeline_core_abi6=true`, старого ключа нет;
- проверка всех локальных ссылок поддерживаемой Markdown-документации;
- визуальная проверка PNG и SVG через browser и renderer без `foreignObject`.

Не прошли по причинам, не связанным с внесёнными числовыми исправлениями:

- `npm run format:check`: 13 ранее неформатированных файлов;
- `npm run lint`: одна ошибка
  [`react-hooks/set-state-in-effect`](../../web/src/ocr/OcrContext.tsx#L182) и
  102 предупреждения, в основном Prettier;
- полный Python pytest suite локально не запущен: system Python имеет `pytest`,
  но не имеет FastAPI, а OCR virtualenv имеет FastAPI, но не имеет `pytest`.
  Изменённый readiness handler проверен прямым runtime-вызовом; CI запускает
  полный suite в OCR test image.
