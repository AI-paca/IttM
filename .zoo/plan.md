# plan.md

Дата: 2026-06-14
База: локальная ветка `Hw5`
Режим: architect / zoo code orchestration

## Правила для всех под-агентов

- Работать только в локальных производных ветках, не в `main`.
- Не писать runtime OCR fixes напрямую в `Hw5`, если это engine-level изменение.
- Для engine-дефектов использовать цикл: `test в Hw5 -> merge в engine-descendant -> fix -> merge обратно в Hw5`.
- `Hw5` и `engine` должны содержать чистые тематические коммиты.
- Все спорные выводы и предложения оформлять в markdown-файлы, а не в чат.
- Если нужен мой выбор, сначала обновить `plan.md` с вариантами и последствиями.

## Что уже выяснено

### Целевая архитектура

- Нужен единый `Extraction contract` для CLI, web, extension и local daemon.
- Browser WASM OCR и Python OCR должны остаться разными executors.
- Унифицировать нужно contracts, profile names, diagnostics и quality invariants.
- Унифицировать preprocessing/filter implementation между Python и WASM, скорее всего, нецелесообразно.
- Gateway должен эволюционировать из proxy в control plane с task lifecycle.

### Первый продуктовый приоритет

Hyprland / CLI-first путь:

```bash
grim -g "$(slurp)" - \
  | curl --data-binary @- http://127.0.0.1:3000/api/extract/text \
  | wl-copy
```

Нужен first-class endpoint для raw image bytes -> `text/plain`, плюс sibling streaming/NDJSON contract для прогресса.

### Оценка текущего кода

Сильные стороны:
- `gateway/src/tasks/task-service.ts`
- `gateway/src/tasks/process-worker.ts`
- `gateway/src/tasks/*.test.ts`
- `ocr/tests/test_layout_pipeline_contracts.py`
- `web/src/ocr/layout-pipeline.test.ts`

Слабые стороны:
- `gateway/src/core/routes.test.ts` в основном glue/smoke, а не архитектурные контракты.
- `ocr/tests/test_main.py` слишком heavily mocked и часто тестирует monkeypatched service вместо боевых boundary.
- `testtables` признан временным A/B corpus, а не фундаментом test platform.

## Что надо сделать дальше

### Поток работ через под-агентов

1. Под-агент `architect-contracts`
   - Подготовить markdown RFC для `ExtractionRequest/Event/Result/Error`.
   - Зафиксировать CLI-first и task API shape.
   - Указать compatibility adapters для `/convert` и `/convert/stream`.

2. Под-агент `architect-tests`
   - Подготовить markdown с test pyramid / test platform.
   - Разделить suites: contract, quality, resource, security, compose.
   - Отдельно перечислить, какие текущие тесты понизить до glue/smoke.

3. Под-агент `architect-branches`
   - Подготовить markdown branch discipline.
   - Описать, какие изменения идут только в `Hw5`, какие только в descendants от `engine`.
   - Прописать правила для других агентов во избежание конфликтов.

4. Под-агент `code` или `debug`
   - После утверждения RFC начать первую реализацию: Hyprland text endpoint + tests.
   - Работать в локальной производной ветке от `Hw5`, например `hw5-cli-text-endpoint`.

## Предварительная структура итоговых markdown-файлов

- `.zoo/plan.md` — текущий orchestration plan и точки выбора.
- `.zoo/.review-from-llm/HYPRLAND_API_RFC.md`
- `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md`
- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md`

## Вопросы, которые надо решать только через обновление этого файла

1. Оставляем ли legacy `/convert` как thin compatibility route, или сразу начинаем переносить UI на `/api/tasks`?
2. Нужен ли текстовый sync endpoint отдельно от task API, или он должен быть просто adapter над task execution?
3. Первая реализация CLI text endpoint должна идти через текущий Python OCR path или сразу через `TaskService + ProcessWorkerExecutor`?

## Моя текущая рекомендация

- Сначала оформить RFC-файлы.
- Затем реализовывать именно `TaskService`-based text endpoint, а не еще один прямой proxy route.
- Legacy `/convert*` оставить как compatibility facade.
- Все следующие агенты явно напоминать: только локальные производные ветки, не трогать `main`, не смешивать test commit и engine fix commit.

## Статус после инкремента code-agent 2026-06-14 20:06 MSK

Короткая архитектурная проверка показывает, что направление правильное:

- `gateway/src/core/routes.ts` теперь сначала отдаёт управление `handleTaskApi()`, а legacy `/convert*` остаётся ниже как compatibility path.
- `gateway/src/tasks/http-api.ts` реализует `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/:id`, `GET /api/tasks/:id/events`, `POST /api/tasks/:id/cancel`.
- Sync режимы `text`, `markdown`, `json` идут через `TaskService`, а не через новый прямой proxy route.
- Raw bytes upload для Hyprland сценария уже поддержан через `request.arrayBuffer()` и `File([body], filename)`.
- `Accept` negotiation соответствует RFC: `text/plain` -> text, `text/markdown` -> markdown, `application/x-ndjson` / `text/event-stream` -> events.
- `Last-Event-ID` исправлен правильно: resume начинается с `lastEventId + 1`, поэтому `Last-Event-ID: 0` не дублирует `accepted`.
- `GET /api/tasks?state=...&limit=...` добавлен как безопасный in-memory listing с сериализацией без file bytes.
- Disconnect в sync path теперь отменяет задачу и возвращает `499`; это правильный минимальный шаг.

Оставшийся архитектурный риск:

- `/api/tasks/:id/events` при client disconnect пока только прекращает stream watcher. Следующий инкремент должен добавить grace-cancel: если единственный events-consumer отвалился и задача ещё running/queued, task отменяется после короткого grace period, если за это время не появился новый watcher.
- Текущий executor `OcrStreamTaskExecutor` всё ещё вызывает Python backend через HTTP stream. Это допустимый промежуточный HW5-шаг, но финальная execution plane всё равно должна прийти к supervised worker/process boundary.

## Обновления RFC

- `.zoo/.review-from-llm/HYPRLAND_API_RFC.md` требует небольшого обновления, но не переписывания: он уже в целом совпадает с кодом; надо только синхронизировать фактические детали listing, sync disconnect `499`, `Last-Event-ID`, и явно отметить, что `OcrStreamTaskExecutor` временно использует backend HTTP stream до supervised worker boundary.
- `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md` отсутствует и должен быть создан отдельным architect/test под-агентом.
- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md` отсутствует и должен быть создан отдельным architect/branch под-агентом.

## Статус после инкремента code-agent 2026-06-14 20:23 MSK

Code-agent завершил инкремент `hw5-events-grace-cancel`. Быстрая архитектурная проверка показывает, что решение соответствует плану:

- `gateway/src/tasks/http-api.ts` добавил `DEFAULT_EVENTS_DISCONNECT_GRACE_MS` и env override `TASK_EVENTS_DISCONNECT_GRACE_MS` без затрагивания Python/OCR runtime.
- `/api/tasks/:id/events` теперь использует отдельный `streamAbort`, активные watcher-счётчики и delayed cancel только при disconnect до terminal event.
- Reconnect/resume до истечения grace отменяет pending cancel через увеличение active watcher count и `clearTimeout`.
- Terminal tasks защищены: cancel планируется только для `queued` и `running`.
- `Last-Event-ID` оставлен в правильной форме `lastEventId + 1`, поэтому `Last-Event-ID: 0` не дублирует `accepted`.
- `resetTaskApiForTests()` очищает timers, что важно для test isolation.
- Проверки от code-agent: `npm test` 144/144, `npm run typecheck`, `npm run lint` прошли.

Архитектурные замечания:

- Default grace `250 ms` хорош для тестируемого локального MVP, но для реального Hyprland/web usage может быть слишком коротким. Значение вынесено в env, поэтому это не блокер.
- `eventStreamWatchStateMaps` нужен только для test cleanup, но это допустимый компромисс для in-memory MVP; durable task store позже должен заменить эту глобальную структуру.
- Следующий риск теперь не events disconnect, а то, что `OcrStreamTaskExecutor` остаётся HTTP adapter к Python backend, а не supervised process boundary. Это осознанный промежуточный шаг.

## RFC-долги после grace-cancel

- `.zoo/.review-from-llm/HYPRLAND_API_RFC.md` нужно обновить фактическими деталями: default grace 250 ms, `TASK_EVENTS_DISCONNECT_GRACE_MS`, watcher reconnect semantics, sync disconnect `499`, listing shape, временный HTTP-backed `OcrStreamTaskExecutor`.
- `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md` всё ещё отсутствует; нужен отдельный под-агент.
- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md` всё ещё отсутствует; нужен отдельный под-агент.

## Следующие инкременты через под-агентов

1. `architect-rfc-sync`: обновить `HYPRLAND_API_RFC.md` под фактический код task API/grace-cancel.
2. `architect-tests`: создать `TEST_PLATFORM_RFC.md` с test pyramid, generated fixtures, contract/resource/security/compose tiers и статусом текущих плохих тестов.
3. `architect-branches`: создать `BRANCH_DISCIPLINE.md` с правилами локальных веток для всех агентов.
4. После RFC-долгов: code-agent для следующего HW5-only шага — либо CLI клиент переводить на `/api/tasks?sync=text/events`, либо готовить compatibility facade `/convert*` поверх `TaskService` вместо прямого `OcrClient` proxy.

## Статус после architect-tests 2026-06-14 20:49 MSK

Создан `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md`.

Ключевые решения:

- Test platform разделена на 7 tiers: pure unit/contract, in-process fake adapters, medium integration, resource, quality matrix, security, Compose E2E.
- `gateway/src/tasks/*`, `ocr/tests/test_layout_pipeline_contracts.py`, `web/src/ocr/layout-pipeline.test.ts`, `web/src/ocr/pipeline-config.test.ts` закреплены как полезные contract-like тесты.
- `gateway/src/core/routes.test.ts` и `ocr/tests/test_main.py` понижены до glue/smoke, а не источника production confidence.
- `testtables/` зафиксирован как ручной A/B corpus, не test platform.
- PR gate должен быть быстрым: Tier 1+2, без реального OCR и без `testtables`, целевой лимит меньше 5 минут.
- Engine-related repro tests живут в `Hw5`, runtime fixes идут только через `engine-descendant` цикл.

Оставшийся RFC-долг:

- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md` всё ещё отсутствует и должен быть создан следующим architect под-агентом.

## Статус после architect-branches 2026-06-14 20:54 MSK

Создан `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md`.

Ключевые решения:

- Все под-агенты работают только в локальных производных ветках; `main` не трогается.
- `Hw5` — integration/test trunk для contracts, task API, CLI, test platform, docs/RFC и test-only repro.
- `engine` — только runtime OCR fixes после появления repro/contract test в `Hw5`.
- HW5-only изменения не переносятся в `engine`, пока конкретный engine-дефект не требует очередного цикла.
- Runtime OCR fix напрямую в `Hw5` запрещён.
- Смешанные коммиты запрещены: test-only, runtime fix, docs/RFC, CI и refactor должны быть разнесены.
- Для engine-дефекта используется цикл: repro в `Hw5` -> локальный descendant `hw5-engine/<problem>`/`engine-<feature>` -> minimal fix -> merge обратно в `Hw5`.
- `.zoo/plan.md` — точка координации, если агенту нужен выбор пользователя или если он меняет направление работ.

## RFC-долги закрыты

Созданы и синхронизированы:

- `.zoo/.review-from-llm/HYPRLAND_API_RFC.md`
- `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md`
- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md`

## Следующий рекомендуемый HW5-only code инкремент

Перевести CLI/headless client на новый task API:

- `scripts/ittm-extract.ts` / `gateway/src/cli/*` должны использовать `/api/tasks?sync=text` для plain output и `/api/tasks?sync=events` или `/api/tasks/:id/events` для progressive output.
- Legacy `/api/convert/stream` оставить как fallback/compatibility path на время миграции.
- Добавить тесты на stdin raw bytes -> task API request, `Accept: text/plain`, cancellation -> `130`, HTTP `499`, partial NDJSON fallback.
- Не менять Python OCR/runtime и не переносить изменения в `engine`.
