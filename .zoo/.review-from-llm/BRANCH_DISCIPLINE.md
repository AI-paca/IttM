# BRANCH_DISCIPLINE.md

Дата: 2026-06-14
База: локальная ветка `Hw5`
Режим: architect / zoo code orchestration
Связанные документы:
[`.zoo/plan.md`](../../plan.md:1),
[`BRANCH_MAP.md`](BRANCH_MAP.md:1),
[`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md:1),
[`TEST_PLATFORM_RFC.md`](TEST_PLATFORM_RFC.md:1),
[`HYPRLAND_API_RFC.md`](HYPRLAND_API_RFC.md:1)

> Этот RFC предназначен для всех под-агентов (architect / code / debug / test /
> docs), работающих с репозиторием. Он не предлагает runtime-фиксов OCR и не
> меняет поведение `pytesseract` / OpenCV / EasyOCR. Цель — зафиксировать
> правила работы с локальными ветками, чтобы `main`, `Hw5` и `engine`
> оставались чистыми точками синхронизации.

## 0. Контекст и зачем это нужно

Текущая модель работы описана в [`.zoo/plan.md`](../../plan.md:7) и
[`BRANCH_MAP.md`](BRANCH_MAP.md:1): пользователь требует, чтобы агенты
работали строго в локальных производных ветках, делили работу между ветками
для уменьшения конфликтов, и не трогали `main`, `engine` runtime и
универсальные trunk-коммиты без явной причины.

Этот документ фиксирует:

1. кто чем владеет (branch ownership matrix);
2. что можно и нельзя коммитить в `Hw5`, `engine`, локальные descendants;
3. правила «чистых» тематических коммитов;
4. когда создавать отдельную локальную ветку;
5. как избегать конфликтов между агентами;
6. как оформлять отчёты в markdown и когда обновлять `.zoo/plan.md`;
7. границу между test-only repro и runtime fix;
8. что запрещено категорически.

## 1. Принципы

1. **`main` нетронут.** Это чистая точка синхронизации. Любой коммит в `main`
   только по явной команде пользователя.
2. **Каждый агент работает в собственной локальной ветке.** Никаких прямых
   коммитов в `Hw5` или `engine` от под-агентов; они делают PR/merge локально
   по согласованию.
3. **`Hw5` и `engine` принимают только тематические «чистые» коммиты.**
   Один логический шаг = один коммит, без смешивания тестов и фиксов,
   без `WIP`, без отладочного мусора.
4. **Разделение доменов.** HW5-only (contracts, task API, CLI, tests, docs) и
   engine-домен (OCR runtime, layout, preprocessing) — это **разные ветки и
   разные коммиты**. См. § 5.
5. **Engine-дефекты идут по циклу.** `test(repro) в Hw5 -> merge в engine-desc
   -> runtime fix -> merge обратно в Hw5`. Никаких прямых runtime-фиксов в
   `Hw5`.
6. **Конфликты решаются ветками, не форс-пушами.** Если два агента правят
   одну зону, они разводятся по разным локальным веткам и потом сводятся
   явным merge.
7. **Markdown-only RFC не требует runtime-решения.** Любой документ
   `.zoo/.review-from-llm/*.md` — самостоятельный артефакт и не должен
   тянуть за собой правки в `gateway/`, `ocr/`, `web/`.
8. **Все спорные выводы и предложения оформляются в markdown, не в чат.**
   Чат используется только для вопросов и подтверждений.

## 2. Branch Ownership Matrix

Эта матрица — канон для всех под-агентов. Если агент не уверен, в какую
ветку коммитить, он смотрит сюда.

| Ветка / категория | Worktree | Базой является | Что можно | Что запрещено | Кто работает |
| --- | --- | --- | --- | --- | --- |
| **`main`** | `/home/alpaca/GitHub/IttM` | upstream | Только merge от пользователя | Прямые коммиты, merge от агентов, runtime OCR-фиксы, тестовые правки | Только пользователь |
| **`Hw5`** (trunk) | `/home/alpaca/GitHub/IttM-Hw5-clean` | `main` | Тестовые/архитектурные коммиты, contracts, task API, CLI, docs, generated fixtures, smoke-suite. Engine-related repro-тесты как `xfail`/skip | Runtime OCR-фиксы, изменения `ocr/app/engines/*`, `ocr/app/layout/*`, `ocr/app/preprocessing.py`, миксы test+fix | Только явный merge от потомков |
| **`engine`** (trunk) | `/home/alpaca/GitHub/IttM-engine-cycle` | `main` | Runtime OCR-фиксы, layout math, engine-specific tests, качественные профили движка | HW5-only contracts, CLI/HTTP API, web UI, docs-обвязку | Только явный merge от потомков |
| **`hw5-<feature>`** | локальная | `Hw5` | Один конкретный HW5-only инкремент (CLI, contract, task API, test platform) | Runtime OCR-фиксы, перенос в `main`, миксы разных тем | code, debug, test агенты |
| **`hw5-engine/<problem>`** | локальная | `Hw5` (через merge) | Минимальный engine-фикс под конкретный repro | HW5-only contracts, web UI, CLI, refactoring вне проблемы | code/debug агенты под engine-домен |
| **`engine-<feature>`** | локальная | `engine` | Engine-фиксы и оптимизации | HW5-only фичи, task API, contracts | code/debug агенты под engine |
| **`rfc/<topic>`** или `docs/<topic>` | локальная | `Hw5` | Только markdown в `.zoo/.review-from-llm/*.md`, `docs/**` | Любые runtime-изменения, package.json, scripts | architect/docs агенты |
| **`code-agent/<task>`** | локальная | `Hw5` (или `hw5-<feature>`) | Конкретная реализация инкремента, описанного в плане | Чужие задачи, engine-фиксы, чужой `package.json` | code-агент под свой инкремент |
| **`test-agent/<tier>`** | локальная | `Hw5` | Тесты Tier 1 / Tier 2 / smoke, fixture seeds, fault-injection contract | Реальный OCR, runtime-фиксы, performance benchmarks | test-агент |
| **`debug-agent/<bug>`** | локальная | `Hw5` или `engine` в зависимости от домена | Repro + минимальный fix или routing в `engine-desc` | Ломать trunk напрямую, миксы | debug-агент |

### 2.1 Правила именования

- `hw5-<короткое-имя>` — общий HW5-only инкремент.
  Пример: `hw5-cli-text-endpoint`, `hw5-events-grace-cancel`,
  `hw5-test-platform-rfc-impl`.
- `hw5-engine/<короткий-id-проблемы>` — engine-фикс под конкретный repro.
  Пример: `hw5-engine/long-receipt-segmentation`,
  `hw5-engine/exif-rotation-regression`.
- `engine-<короткое-имя>` — фикс на trunk-движке.
  Пример: `engine-tesseract-quality`, `engine-time-optimization`.
- `rfc/<topic>` — только markdown RFC.
  Пример: `rfc/extraction-contract`, `rfc/hyprland-api`.
- `docs/<topic>` — документация (`docs/**`).
  Пример: `docs/architecture-update`.
- `code-agent/<task>` — реализация, выданная architect'ом.
  Пример: `code-agent/grace-cancel`.
- `test-agent/<tier>` — тестовая работа.
  Пример: `test-agent/tier-2-contracts`, `test-agent/smoke-tags`.
- `debug-agent/<bug>` — отладка.
  Пример: `debug-agent/pdf-spool-leak`.

Имя должно сразу говорить, **что** меняется. Не использовать
`tmp`, `wip`, `experiment`, `untitled` — это анти-паттерн.

## 3. Что можно и нельзя коммитить

### 3.1 В `Hw5` (trunk)

**Можно:**

- Application contract: `ExtractionRequest/Event/Result/Error` и их сериализаторы
  (см. [`HYPRLAND_API_RFC.md`](HYPRLAND_API_RFC.md:51)).
- Task API: `POST /api/tasks`, `GET /api/tasks`, `GET /api/tasks/:id`,
  `GET /api/tasks/:id/events`, `POST /api/tasks/:id/cancel`, sync `text` /
  `markdown` / `json` / `events`.
- Compatibility adapters для legacy `/convert` и `/convert/stream` (тонкие
  обёртки поверх `TaskService`).
- CLI-обвязка: `scripts/ittm-extract.ts`, `gateway/src/cli/*`.
- Web UI над `ExtractionClient` (если UI уже в trunk, иначе — `web-ui`).
- Тесты: Tier 1 + Tier 2 (см. [`TEST_PLATFORM_RFC.md`](TEST_PLATFORM_RFC.md:5)),
  fixture seeds, fault-injection contract tests.
- Generated fixtures: `ocr/tests/generated_media.py`,
  `ocr/tests/document_templates.py`, `ocr/tests/quality_fixtures.py`,
  `web/src/ocr/*-pipeline*.test.ts` (browser rasters).
- Документация: `docs/ru/**`, `docs/en/**`, `.zoo/**`, RFC-файлы.
- Smoke suite для Compose (Tier 7 smoke), если не меняет runtime.
- Repro-тесты для **архитектурных** дефектов (lifecycle, cancel, transport,
  security policy) и для **engine-дефектов**, оформленные как
  `xfail`/`expected_failure`/`skip` (см. § 6).
- Testability hooks: например, `resetTaskApiForTests()`,
  `resetTaskServiceForTests()`, `clearInputStorageForTests()`.

**Нельзя:**

- Менять runtime OCR (никаких фиксов в `ocr/app/engines/*`,
  `ocr/app/layout/*`, `ocr/app/preprocessing.py`, `ocr/app/chunking/*`,
  `ocr/app/formatting/*`).
- Делать runtime-фиксы «по-быстрому» в `Hw5` с пометкой «это маленький
  engine-fix, потом перенесём». Не перенесём. Это источник смешанных
  коммитов.
- Переносить HW5-only contracts / task API / CLI / test platform / docs в
  `engine` без явного engine-дефекта.
- Миксовать в одном коммите test-only и runtime-фикс.
- Миксовать в одном коммите разные темы (например, «fix task API + добавил
  новый CLI флаг + переписал smoke»).
- Заливать `WIP`, `TODO`, отладочный `console.log`, закомментированный код.

### 3.2 В `engine` (trunk)

**Можно:**

- Runtime OCR-фиксы, пришедшие через цикл из `Hw5` (см. § 7).
- Engine-specific профили: `ocr/app/engines/*`, `ocr/app/layout/*`,
  `ocr/app/preprocessing.py`.
- Качественные эксперименты: `engine-tesseract-quality`,
  `engine-time-optimization`.
- Engine-specific тесты, помеченные `requires_engine` и не идущие в HW5 PR
  gate.
- Stage timings, resource snapshots, soak-прогоны.

**Нельзя:**

- HW5-only contracts: нельзя добавлять поля в `ExtractionRequest`,
  `ExtractionEvent`, `ExtractionError`, не привязанные к активному
  engine-дефекту.
- Web UI, CLI, task API, generated fixtures, security policies.
- Refactoring, не относящийся к активному engine-фиксу.
- Прямой merge из `main` без цикла.

### 3.3 В локальных descendants

**Можно всё, что разрешено для базы + в рамках темы ветки.** Один инкремент —
одна ветка. Не тащить в `hw5-cli-text-endpoint` engine-фиксы. Не тащить в
`hw5-engine/<problem>` UI-правки.

**Нельзя:**

- Делать merge в `main`.
- Менять `package.json`/`requirements*.txt`/`tsconfig.json`/`eslint.config.js`
  за рамками своей задачи (это системный файл — менять явно, отдельным
  коммитом, с описанием «build dep: <причина>»).
- Менять конфигурацию CI за пределами своего тира.
- Делать push в удалённый репозиторий от имени агента (только локальные
  коммиты и локальные merge).

## 4. Правила «чистых» коммитов

### 4.1 Один коммит — одна тема

Плохо:

```text
- chore: small fixes
- fix stuff
- WIP
- more changes
```

Хорошо:

```text
- feat(tasks): add /api/tasks?sync=text endpoint
- test(contract): add cancel race coverage
- refactor(routes): split handleTaskApi from legacy /convert path
- docs(plan): record hw5-events-grace-cancel increment
```

Префиксы conventional commits обязательны для кода:

- `feat:`, `fix:`, `refactor:`, `perf:`, `test:`, `chore:`, `docs:`,
  `build:`, `ci:`.
- Scope обязателен: `feat(tasks):`, `test(contract):`, `fix(ocr):`.
- Тело коммита: что изменилось и **почему** (не «как»).
- Если меняется API или контракт, в теле ссылка на RFC
  (`.zoo/.review-from-llm/HYPRLAND_API_RFC.md`).

### 4.2 Запрет на смешанные коммиты

Запрещено:

- объединять test + fix в одном коммите;
- объединять runtime OCR change + HW5 contract change;
- объединять docs + code;
- объединять две независимые темы «потому что я тут»;
- коммитить «вместе с» (например, «+ подправил lint» в основной фикс).

Если коммит перестаёт быть атомарным, нужно сделать `git reset`/interactive
rebase до точки расхождения и разнести.

### 4.3 Запрет на «грязные» коммиты

- Никаких `WIP`, `tmp`, `fix lint`, `oops`, `revert`, `squash me`.
- Никаких `console.log` / `print(...)` / `pdb.set_trace()` в коде trunk'а.
  Debug-вывод в feature-ветке допустим, но удаляется до merge.
- Никаких закомментированных блоков «на всякий случай».
- Никаких больших бинарных файлов, готовых PDF, сканов, `testtables/`
  corpus — это A/B материал, не часть кода.
- `git status` перед коммитом должен быть чистым: только то, что
  относится к теме.

### 4.4 Атомарность и обратимость

Каждый коммит должен:

- компилироваться (TypeScript / Python) сам по себе или иметь явную
  пометку, почему он временно сломан (например, «split: feat добавляет
  заглушку, feat(executor) её реализует» — оба коммита в одной ветке
  идут подряд);
- проходить CI Tier 1 + Tier 2 (см.
  [`TEST_PLATFORM_RFC.md`](TEST_PLATFORM_RFC.md:5));
- иметь читаемый `git log -1` без `git show` (то есть заголовок + тело
  достаточно, чтобы понять суть).

## 5. Когда создавать отдельную локальную ветку

### 5.1 Обязательные случаи

Создавать новую локальную ветку **обязательно**, если:

1. **Новая фича / RFC / крупный refactor.** Например,
   `hw5-cli-text-endpoint` — это новая функциональность. Прямо в `Hw5`
   нести нельзя.
2. **Изменение затрагивает > 1 файл и меняет поведение.** Даже если
   изменение маленькое, оно должно идти отдельным PR/merge, чтобы trunk
   оставался чистым.
3. **Engine-дефект.** Цикл § 7 обязывает использовать
   `hw5-engine/<problem>` или descendants от `engine`.
4. **Изменения только в документации / RFC.** Отдельная `rfc/<topic>` или
   `docs/<topic>` ветка нужна, чтобы не путать doc-only и code.
5. **Код-агент получил инкремент от architect'а.** У каждого code-agent
   своя ветка `code-agent/<task>`, чтобы не пересекаться с другими
   code-агентами.
6. **Test-agent работает с конкретным тиром.** `test-agent/<tier>`.
7. **Debug-agent изолирует repro.** `debug-agent/<bug>`.

### 5.2 Когда НЕ нужно создавать ветку

- Правка опечатки в комментарии или `docs/**` без изменения смысла —
  допустимо править прямо в рабочей `hw5-<feature>` ветке, если её ведёт
  тот же агент. Не создавать отдельную ветку на одну строку.
- Микро-правка в `.zoo/plan.md` для отражения статуса (например, «status
  after increment X») — в той же `rfc/<topic>` или `hw5-<feature>` ветке.
- Добавление одной строки в `package.json`/`requirements*.txt` как часть
  уже идущего инкремента — внутри той же ветки, отдельным коммитом
  `build(deps): <причина>`.

### 5.3 Когда работа в trunk допустима

**Никогда.** Под-агент не пишет напрямую в `Hw5` или `engine`. Только через
merge из локальной ветки по согласованию. Исключение: пользователь явно
просит «закоммить прямо в `Hw5`», и это узкая задача (например, «добавь
строку в `.zoo/plan.md` прямо в `Hw5`»). В этом случае агент фиксирует
согласие пользователя в markdown-отчёте.

## 6. Test-only repro vs runtime fix

Эта граница — самая частая точка конфликтов. См.
[`TEST_PLATFORM_RFC.md`](TEST_PLATFORM_RFC.md:9).

### 6.1 Test-only repro — коммитится в `Hw5`

Это воспроизводящий тест, который **описывает** дефект, но не лечит его.
Он помечается одним из способов:

- Python: `@pytest.mark.xfail(reason="engine: <короткий id>")`
  или `pytest.mark.skip(reason=...)`.
- TypeScript: `test.skip(...)`, `test.fails(...)` (Node test runner),
  комментарий `// xfail(engine): <короткий id>` рядом с `it/test`.

Примеры test-only коммитов в `Hw5`:

- `test(contract): add cancel race for queued->running transition`
- `test(quality): xfail long-receipt segmentation regression`
- `test(resource): xfail OOM on 80MP dewarp (engine: dewarp-budget)`

Правила:

- Тест компилируется и запускается в `pytest` / `node:test`.
- В Tier 1 / Tier 2 / Tier 3 он **помечен** так, что CI зелёный даже
  при падении.
- В теле коммита — короткая ссылка на problem id (например,
  `engine-dewarp-budget`).
- Сам по себе он не правит production-код, кроме случая, когда
  правка — это testability hook (`resetTaskApiForTests()` и т. п.).

### 6.2 Runtime fix — коммитится в `engine-descendant`

Это лечение, которое идёт **только** в `engine`-домене:

- `ocr/app/engines/*` (Tesseract, EasyOCR, auto)
- `ocr/app/layout/*` (stages, features, selectors, table_formatters,
  pipeline, contracts)
- `ocr/app/preprocessing.py`
- `ocr/app/chunking/*`
- `ocr/app/formatting/markdown_formatter.py`
- `ocr/app/services/convert_service.py` (только в части движка, не
  HTTP-маршрутизации)

Правила:

- Минимальный диф: меньше строк, легче revert.
- Сопровождается engine-тестом или явным комментарием «engine-тест уже
  в `Hw5` под id `<id>`».
- Не приносит HW5-only контракты.
- Не ломает shared `extraction` API.

### 6.3 Запрет на «прямой» engine-фикс в `Hw5`

Типичный антипаттерн:

> «Тут маленькая проблема в `ocr/app/preprocessing.py`, поправлю прямо
> в `Hw5`, потом отдельно перенесу в `engine`.»

Это запрещено, потому что:

- trunk `Hw5` получает несогласованный с `engine` код;
- ломается цикл repro -> fix -> repro;
- при последующем merge `engine -> Hw5` будут конфликты и сюрпризы;
- нарушается rule «`Hw5` принимает только тематические коммиты».

Правильный путь: см. § 7.

## 7. Engine-дефектный цикл (test -> fix -> merge)

Соответствует [`BRANCH_MAP.md`](BRANCH_MAP.md:15) и
[`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md:139).

```text
main
  |\
  | +-- Hw5: test(repro)        <- коммит (1), test-only
  |       \
  +-------- merge Hw5 -> engine-desc  <- коммит (2), merge commit, --no-ff
             |
             +-- engine-desc: fix(problem)  <- коммит (3), runtime fix
                     \
                      merge engine-desc -> Hw5  <- коммит (4), --no-ff
```

### 7.1 Шаги

1. **В `Hw5` появляется test-only repro-коммит.** Помечен `xfail` /
   `expected_failure` / `skip`, ссылается на короткий problem id
   (например, `engine: dewarp-budget`).
2. **Merge в engine-descendant.** `git checkout engine` →
   `git merge --no-ff Hw5 -m "merge: bring repro <id> from Hw5"`.
   Это создаёт видимую границу цикла.
3. **Runtime fix отдельным коммитом.** В `engine`-ветке
   (например, `hw5-engine/dewarp-budget` или прямо в `engine`,
   если fix простой). Сообщение: `fix(ocr): <короткий id> — <что
   именно>`.
4. **Engine тесты зелёные.** Прогон engine-specific suite
   (см. § 3.2).
5. **Merge обратно в `Hw5`.** `git checkout Hw5` →
   `git merge --no-ff engine-desc -m "merge: bring <id> fix from engine"`.
6. **Repro переводится из `xfail` в активный тест** отдельным
   коммитом в `Hw5`: `test(contract): enable repro for <id>`.
   Это и есть «merge обратно», зафиксированный тестом.
7. **Следующий engine-дефект начинает новый цикл** с шага 1.

### 7.2 Запрет на линейное «догоняние»

Запрещено:

- делать `git rebase engine onto Hw5` без merge-границ;
- линейно основывать `engine` на голове `Hw5` без видимых merge-коммитов;
- пропускать шаг merge в `engine` и сразу фиксить в `Hw5`;
- пропускать шаг merge обратно в `Hw5` и оставлять repro `xfail` навечно.

## 8. Conflict-avoidance между агентами

### 8.1 Разделение по тематическим веткам

Если два агента могут править одну зону, они **заранее** расходятся по
разным локальным веткам. Примеры:

- `code-agent/cli-text` и `code-agent/cli-events` — разные синк-режимы,
  разные ветки.
- `test-agent/tier-2-contracts` и `test-agent/tier-2-fault-injection` —
  разные ветки, потом явный merge.
- `debug-agent/pdf-spool` и `debug-agent/upload-limit` — разные ветки,
  даже если оба трогают `ocr/app/routers/convert.py`.

### 8.2 Правило «один файл — один агент в один момент»

Если файл точно пересечётся (например, `gateway/src/tasks/http-api.ts`
правит и `code-agent/grace-cancel`, и `code-agent/listing-serializer`),
то:

1. architect фиксирует в `.zoo/plan.md`, кто за что отвечает;
2. второй агент ждёт merge первого или выбирает соседнюю зону
   (например, новый endpoint или новый тест-файл);
3. merge идёт **последовательно**, не параллельно.

### 8.3 Уведомления в `.zoo/plan.md`

`.zoo/plan.md` — единственное место, где фиксируется текущее
распределение. Перед началом инкремента агент:

1. открывает `.zoo/plan.md`;
2. если его задача уже описана — отмечает «in progress»;
3. если его задача пересекается с уже идущей — явно договаривается
   через `switch_mode` или `ask_followup_question`;
4. по завершении — добавляет секцию «Статус после инкремента <agent>
   <date> <time>» с коротким отчётом.

### 8.4 Никаких force-push в локальных trunk'ах

`git push --force-with-lease` в `Hw5` или `engine` запрещён. История
должна быть линейно-читаемой с merge-границами. Force-push допустим
только в собственной feature-ветке до её merge в trunk.

### 8.5 Никаких параллельных правок одного и того же файла

Если агенты правят один файл параллельно, merge станет кошмаром. Правило:
**один файл в один момент времени правит один агент**. Architect
распределяет зоны.

## 9. Markdown-отчёты и обновление `.zoo/plan.md`

### 9.1 Когда создавать markdown-файл

- Новый RFC: `.zoo/.review-from-llm/<TOPIC>.md`.
- Архитектурное решение, выходящее за пределы одного PR: отдельный файл
  в `.zoo/.review-from-llm/` или `docs/ru/`.
- Сводка по инкременту с нетривиальными последствиями: добавить секцию
  в `.zoo/plan.md`.
- Любой «спорный» вывод, не помещающийся в commit message — markdown
  (не чат).

### 9.2 Когда обновлять `.zoo/plan.md`

`.zoo/plan.md` обновляется, если:

1. agent завершил инкремент, влияющий на roadmap (добавить секцию
   «Статус после инкремента <agent> <date> <time>»);
2. изменились приоритеты или порядок;
3. появились новые риски или блокеры;
4. требуется решение пользователя (записать в «Вопросы, которые надо
   решать»);
5. изменилась branch ownership или RFC-долги.

`.zoo/plan.md` **не** обновляется ради косметики; правки должны нести
новое знание.

### 9.3 Формат отчёта агента

Короткий отчёт после инкремента:

```markdown
## Статус после инкремента <agent-role> YYYY-MM-DD HH:MM MSK

- Что сделано: <1-3 пункта>.
- Какие файлы тронуты: <список, не содержимое>.
- Какие тесты прогнаны: <npm test / pytest / typecheck / lint — с
  числом passed>.
- Известные риски: <если есть>.
- Что нужно дальше: <если есть>.
```

Если инкремент нетривиальный, отчёт идёт отдельным markdown-файлом в
`.zoo/.review-from-llm/`.

### 9.4 Где живут RFC

- `.zoo/plan.md` — текущий orchestration plan и точки выбора.
- `.zoo/.review-from-llm/HYPRLAND_API_RFC.md` — Extraction contract и
  Hyprland/CLI-first API.
- `.zoo/.review-from-llm/TEST_PLATFORM_RFC.md` — test platform tiers и
  runner layout.
- `.zoo/.review-from-llm/BRANCH_DISCIPLINE.md` — этот файл.
- `.zoo/.review-from-llm/TARGET_ARCHITECTURE.md` — целевая архитектура
  после Engine PR.
- `.zoo/.review-from-llm/BRANCH_MAP.md` — карта локальных веток.

Любые новые RFC-файлы создаются в `.zoo/.review-from-llm/`. Имя — в
UPPER_SNAKE_CASE, как `BRANCH_DISCIPLINE.md`.

## 10. Категорические запреты

Эти правила не имеют исключений, кроме явной команды пользователя.

1. **Никаких прямых коммитов в `main`.** Только пользователь.
2. **Никаких runtime OCR-фиксов в `Hw5`.** Только через `engine-desc`.
3. **Никаких смешанных коммитов.** Один коммит — одна тема. Test + fix
   раздельно. Docs + code раздельно.
4. **Никакого переноса HW5-only contracts / task API / CLI / test
   platform / docs в `engine` без engine-дефекта.** Это значит: если
   появляется новое поле в `ExtractionRequest`, оно не идёт в `engine`,
   пока не появился repro, требующий этого поля в `engine`-зоне.
5. **Никаких force-push в trunk'ах** (`Hw5`, `engine`).
6. **Никаких «WIP» / «tmp» / «fix lint» коммитов в trunk.** Только
   атомарные тематические коммиты.
7. **Никаких `console.log` / отладочного мусора в trunk-коде.** Debug
   разрешён в feature-ветке, удаляется до merge.
8. **Никаких изменений `package.json` / `requirements*.txt` /
   `tsconfig.json` / `eslint.config.js` без явного обоснования в
   commit message.** Эти файлы — общие; правка в feature-ветке
   обязана объяснять «build dep: <причина>» или
   «tooling: <причина>».
9. **Никаких готовых PDF, сканов, `testtables/` corpus в Git.** Это
   A/B материал, не часть кода.
10. **Никаких live DOM сторонних сайтов как контракта.** Это явно
    запрещено в [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md:160).
11. **Никаких ретраев внутри sync endpoint'а** (см.
    [`HYPRLAND_API_RFC.md`](HYPRLAND_API_RFC.md:399)).
12. **Никаких «объединений» Python preprocessing и Browser
    preprocessing.** Общий — только контракт, не код
    ([`HYPRLAND_API_RFC.md`](HYPRLAND_API_RFC.md:510)).
13. **Никаких «тестов на monkeypatched stub» как production
    confidence.** `routes.test.ts` и `test_main.py` помечены как
    smoke, контрактная роль — у `task-service.test.ts`,
    `process-worker.test.ts`, `test_layout_pipeline_contracts.py`
    ([`TEST_PLATFORM_RFC.md`](TEST_PLATFORM_RFC.md:286)).
14. **Никаких абсолютных требований coverage (85%, 99.9%) как KPI.**
    См. [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md:164).
15. **Никаких «голословных» утверждений в markdown RFC.** Любой
    performance / quality / memory claim обязан ссылаться на тест,
    документ, бенчмарк или явное измерение.

## 11. Чек-лист перед merge в `Hw5` или `engine`

Под-агент, готовый отдать свой инкремент на merge в trunk, проходит
следующий чек-лист:

- [ ] Все коммиты атомарные, conventional-commits с scope.
- [ ] Нет смешанных test+fix / docs+code коммитов.
- [ ] `git status` чистый, нет untracked / debug-вывода.
- [ ] `npm test` / `pytest` зелёные для затронутой зоны.
- [ ] `npm run typecheck` / `npm run lint` зелёные (для TS-зоны).
- [ ] Если менялся runtime OCR — это запрещено в `Hw5`, идёт в
  `engine-desc`.
- [ ] Если менялся contract / API / CLI / docs — это HW5-only.
- [ ] Если есть repro-тест — он помечен `xfail`/`skip`/`expected_failure`
  с коротким problem id.
- [ ] `package.json` / `requirements*.txt` / `tsconfig.json` /
  `eslint.config.js` не тронуты, либо изменения явно описаны.
- [ ] Markdown-отчёт в `.zoo/plan.md` (или отдельный файл в
  `.zoo/.review-from-llm/`) обновлён.
- [ ] `BRANCH_MAP.md` синхронизирован с фактическим состоянием
  (если появилась новая ветка или цикл).
- [ ] Все «TODO» в коде — это либо явный production-TODO с
  описанием, либо testability hook; не «забытый» мусор.
- [ ] Никаких force-push; merge в trunk — `git merge --no-ff`.

## 12. Чек-лист для architect'а при распределении инкрементов

Architect (или mode, замещающий architect) при выдаче инкремента
code/debug/test-агенту:

- [ ] Задача описана в `.zoo/plan.md` со scope, базой, expected commits.
- [ ] Указано, в какой локальной ветке работать (имя).
- [ ] Указано, какие файлы зоны менять можно и какие нельзя.
- [ ] Если есть engine-фикс — отдельный инкремент, отдельная ветка
  `hw5-engine/<problem>`.
- [ ] Если есть пересечение с уже идущим инкрементом — явно отмечено,
  кто за что отвечает.
- [ ] Если нужны новые markdown-файлы — указано имя и расположение.
- [ ] Если меняется `.zoo/plan.md` — указано, какие секции трогать.
- [ ] Acceptance criteria: какие тесты должны пройти, какие команды
  прогнать.

## 13. Краткая сводка

- `main` — только пользователь, чистая точка синхронизации.
- `Hw5` — contracts, task API, CLI, test platform, docs, generated
  fixtures, **не** runtime OCR.
- `engine` — runtime OCR, layout, preprocessing, engine-specific
  профили, **не** HW5-only contracts.
- Engine-дефекты: `test(repro) в Hw5 -> merge в engine-desc -> fix ->
  merge обратно в Hw5`.
- HW5-only contracts / task API / CLI / test platform / docs не идут в
  `engine` без активного engine-дефекта.
- Каждый агент работает в собственной локальной ветке; конфликты
  избегаются разделением по темам, не форс-пушами.
- Один коммит — одна тема; test + fix раздельно.
- Repro-тесты помечаются `xfail`/`skip`/`expected_failure` с коротким
  problem id и мигрируют через engine-цикл.
- Все спорные выводы и предложения — в markdown, не в чат; `.zoo/plan.md`
  обновляется при нетривиальных изменениях.
- Этот RFC не предлагает runtime-фиксов OCR и не меняет поведение
  `pytesseract` / OpenCV / EasyOCR.
