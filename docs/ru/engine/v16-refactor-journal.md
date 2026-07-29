# Журнал подготовки OCR v16

## Цель

Перестроить движок как единый набор сериализуемых этапов, пригодный для
эталонной Python-реализации и последующего Rust/WASM core. Улучшение считается
доказанным только по метрикам выбранных canary-файлов. Подстановки эталонного
текста и эвристики, называющие конкретную фикстуру, запрещены.

Рабочая ветка: `refactor/atomic-recursive-pipeline`.

## Canary-набор

Цели улучшения:

| Canary | Движок/путь | v9 | v13 | Минимальная цель v16 |
|---|---|---:|---:|---:|
| Adobe Scan Oct 26, native PDF | tesseract | 56.8 | 12.7 | >56.8 |
| Adobe Scan Oct 26, raster page 3 | tesseract | 44.1 | 12.8 | >44.1 |
| Adobe Scan Oct 26, raster page 4 | tesseract | 82.1 | 10.3 | >82.1 |
| Adobe Scan Oct 26, raster page 5 | tesseract | 80.6 | 3.0 | >80.6 |
| `photo_2026-06-26_19-56-47.jpg` | tesseract | 99.5 | 61.4 | >=99.5 |
| `photo_2026-06-26_19-56-47.jpg` | easyocr | 99.5 | 49.1 | >=99.5 |
| `photo_6_2026-05-12_22-26-36.jpg` | tesseract | 100.0 | 37.7 | 100.0 без canned output |
| `photo_6_2026-05-12_22-26-36.jpg` | easyocr | 100.0 | 28.4 | 100.0 без canned output |

Защитные canary:

| Canary | Требование v16 |
|---|---:|
| SAMPLE mixed, native PDF | >=93.6 |
| SAMPLE mixed, raster tesseract/easyocr | >=93.6 |
| Ucheb_plan raster page 2, tesseract | >=91.0 |
| UchebPlan и 09.03.03 с native text layer | 100.0 |

Значения взяты из сохранённых `result.csv` v9/v13 и
`scripts/debug/compare_v1_v15.py`. Полный benchmark во время разработки не
запускается.

### Provenance v9

- Контрольный runtime snapshot: `0ed947e` от 05.07.2026 11:15 +03:00.
- Последний содержательный OCR-коммит snapshot: `689d7fc` (`Repair curriculum
  summary tables`). Сам `0ed947e` меняет только audit-документ.
- Native result завершён 05.07 в 11:56, raster result — в 14:43.
- `66b364d` от 15:04 фиксирует, что первые две фазы v9 уже завершены, а третья
  ещё выполняется. Image result завершён в 15:44.
- Промежуточный `6c2706f` меняет только Markdown UI; OCR-дерево между
  `0ed947e` и каноническим audit-коммитом `66b364d` эквивалентно.

Для поиска регрессий используется `git diff 0ed947e..HEAD`, а не приблизительно
выбранная ветка или версия по названию каталога.

## Контракт целевого pipeline

1. `ImagePlane -> AlignmentResult`
2. `AlignmentResult -> SegmentTree`
3. `SegmentTree -> SparseProjection`
4. `SparseProjection -> StructuralClassification`
5. `Segment + RecognitionContext -> RecognitionCandidates`
6. `RecognitionCandidates -> SelectedText`
7. `StructuralBlock[] -> StructuralRecord[]`
8. `StructuralRecord[] -> MarkdownDocument`

Декодирование PDF, OCR и small LLM являются платформенными портами. Геометрия,
sparse-коды, agenda языков, scoring кандидатов и структурное объединение должны
стать переносимым детерминированным core.

## Изменения

### `4173305 refactor(ocr): make recursive layout stages atomic`

- Добавлен `RecursiveGridAnalysis`: листья, sparse projection, стабильная
  signature и classification передаются как один неизменяемый результат.
- `layout/stages.py` больше не пересобирает эти этапы независимо.
- Добавлен атомарный тест порядка и согласованности projection/signature/profile.
- Проверка: 87 passed, 6 skipped на layout/sparse/language наборе; расширенный
  baseline до изменения: 137 passed, 6 skipped.

Первичная гипотеза о геометрической причине Adobe-регрессии была опровергнута
точным differential trace с v9 и сохранена здесь как важная поправка. Для Adobe
page-004 оба snapshot дают 13 leaves, sparse shape 17x4, 10 codes, профиль
`mixed` и ноль table regions. Высокий результат v9 создавался recognition/plain
fallback, а не table classification.

### Восстановление Adobe language candidates

- Удалён оставшийся identity-hook `recover_known_ocr_phrases`; production больше
  не содержит точки расширения для canned OCR-подстановок.
- Нормальный кириллический текст с иностранным термином больше не считается
  mixed-script noise при наличии устойчивого кириллического контекста.
- Один и тот же mixed-script/tech штраф больше не применяется второй раз при
  оценке word candidate.
- `90x60/32`, `936/1`, `007/2011` больше не считаются code paths.
- Естественные смешанные слова с одним дефисом не считаются повреждёнными
  путями без расширения, корня, slash/backslash или underscore.

Короткий реальный Tesseract-прогон после исправления:

| Canary | v13 | После исправления | v9 |
|---|---:|---:|---:|
| Adobe raster page 3 | 12.8 | 44.10 | 44.1 |
| Adobe raster page 4 | 10.3 | 82.08 | 82.1 |
| Adobe raster page 5 | 3.0 | 80.61 | 80.6 |

Артефакты: `debug/artifacts/v16-adobe-language-fix*` и
`debug/artifacts/v16-adobe-language-path-fix`. На этих страницах остаётся
`markdown_grammar=0`, поэтому следующий прирост выше v9 должен происходить в
structural grammar, не через изменение уже восстановленного текста.

### Отклонённый эксперимент: outline после flatten

Эксперимент `df37cbc` добавлял H2 для uppercase-строки уже в
`MarkdownFormatter` и поднимал grammar-score Adobe pages 3/4. Подход признан
неверным: после flatten потеряны соседние сегменты и геометрия, поэтому регистр
OCR не является достаточным структурным сигналом. Изменение отменено следующим
коммитом и не входит в ожидаемые результаты v16.

Заголовки должны создаваться только structural grammar по блоку короче 90
символов с пустым структурным соседом сверху и снизу. Если isolation неизвестна,
движок не должен угадывать тип блока.

## План следующих итераций

- записать сериализуемый trace каждого атомарного этапа для выбранных страниц;
- удалить canned-восстановление диаграммы из OCR corrections;
- отделить generic candidate scoring от `convert_service.py`;
- добавить одинаковые structural records и sparse-коды для Python и browser;
- сравнивать одну гипотезу за запуск, сохраняя result/trace рядом с записью;
- перед v16 перечислить все коммиты, команды, ожидаемые дельты и риски.

## Единый pipeline core

Первый production-переход вынесен из `convert_service.py` в
`app/pipeline_core`:

- `RecognizedSegment` — immutable image-free результат recognition;
- `RecognitionBatch` — сегменты, totals, flags и versioned stage trace;
- `StructuralRenderArtifact` — typed результат grammar с отдельными полями
  `bypassed`, `lossy_merge_rejected`, `lint_pass`, `confirmed`;
- `PipelineCapabilities` и `recipe_for()` строят recipe адаптера;
- trusted OCR/VLM API с готовым Markdown получает recipe только из
  `recognize_segments`, без local alignment, T9, lexical correction и grammar.

Вызовы между этапами являются обычными in-process функциями. JSON-сериализация
требуется только для trace, golden tests и будущего Rust/WASM ABI, а не для
HTTP/IPC между модулями.

Production `_convert_page_segment()` теперь выполняет region OCR через
`recognize_segments()` и передаёт structural grammar только image-free records.
Решения о fallback больше не читают строки runtime flags: они используют typed
поля `StructuralRenderArtifact`. Flags остаются диагностическим output.

### Ownership legacy flags

Добавлен исчерпывающий реестр всех 51 публикуемых profile flags:

- каждый key имеет ровно одного владельца-этап;
- для trusted API указано `bypass` или `plain_text_only`;
- тест сравнивает registry с фактическими flags всех профилей;
- неизвестный или дублированный key ломает тест вместо тихого игнорирования.

Этапный debug-инструмент поддерживает `--layout-only` и пишет
`02-recursive-grid-trace.json`: alignment, leaves, sparse projection и profile
без запуска OCR. Первый trace сохранён в
`debug/artifacts/v16-stage-traces/adobe-page004`.

## Rust/WASM core ABI v1

Добавлен dependency-free crate `pipeline-core/` с обязательными контрактами:

```text
ImagePlane
  -> Segment[]
  -> SparseProjection
  -> RecognizedSegment[]
  -> StructuralRecord[]
  -> StructuralRenderArtifact
```

В Rust перенесены deterministic sparse-code algebra (`+3`, `+5`, `+11`) и
capability recipe. Raw C ABI экспортирует версию, sparse operation и recipe
mask; это позволяет использовать один маленький `.wasm` без Pyodide и без
wasm-bindgen runtime.

Одна команда `scripts/runtime/build-pipeline-core.sh` выполняет:

1. native Rust tests;
2. exhaustive Python/Rust parity для sparse combinations и 16 recipes;
3. release-сборку `wasm32-unknown-unknown`;
4. загрузку `.wasm` через Node `WebAssembly.instantiate` и проверку exports.

Проверенный результат: 5 Rust tests, Python/Rust parity pass, WASM exports
pass; текущий release artifact — 558 bytes до добавления data contracts.
`rust-wasm` для Arch загружается в пользовательский cache с закреплённым
SHA-256; fallback container закреплён по image digest. Системные пакеты не
изменяются.

### Python native adapter

`app.pipeline_core.native` загружает native core через `ctypes`, проверяет ABI
version и предоставляет sparse/recipe operations. Python `add_sparse_signal()`
и `recipe_for()` вызывают Rust exports при наличии библиотеки; reference fallback
покрыт отдельным тестом для сред разработки без native artifact.

`docker/ocr.Dockerfile` собирает `.so` в закреплённой Rust build-stage и копирует
его в `/opt/ittm-pipeline-core`, вне `/app/app`, чтобы debug source mount не
скрывал core. Compose и CI OCR build context переведены на корень репозитория.
Rust builder-stage проверена. Полный `app-base` smoke был остановлен внешним DNS
Debian mirror во время `apt update`; проверка runtime image остаётся обязательной
перед handoff v16.

### Typed page and PDF text-layer results

`PageSegmentArtifact` переносит markdown, counters и состояние structural lint
между этапами без обратного разбора `runtime_flags`. `PdfTextLayerArtifact`
явно сообщает выбранный layout step. Строковые flags сохранены только как
диагностика/API metadata; старый tuple shape временно принимается на границе для
совместимости тестовых и внешних adapters.

Override API теперь публикует typed `supported_overrides` с допустимыми modes и
status. Историческая связь `lexical_correction -> ocr_language_retry` пока
сохранена и явно помечена `legacy_coupled_language_retry`; если оба override
переданы, независимый `ocr_language_retry` применяется последним независимо от
порядка строк во входе. Неизвестные/экспериментальные ключи по-прежнему fail
closed.

`input_flag_registry` теперь содержит реальные пути источников в
`OcrPipelineProfile` (`image_preprocessing`, `layout.allowed_stages`,
`layout.default_parameters[...]`, plural tuple fields), а не автоматически
сгенерированные несуществующие имена. Тест разрешает каждый опубликованный flag
через declared field path для всех профилей.

### Browser lite loads the shared core

`web/src/ocr/pipeline-core.ts` загружает raw WASM ABI относительно Vite
`BASE_URL`, проверяет ABI version и преобразует recipe mask в typed stage set.
Browser Tesseract теперь разрешает language review, lexical correction и
Markdown grammar через этот recipe, а не только через локальные profile flags.
Trusted Markdown recipe покрыт контрактом: local T9/lexical/grammar stages в
нём отсутствуют.

`build:web` имеет обязательный prebuild shared core; Pages verifier проверяет
наличие, HTTP route и реальную инстанциацию `dist/wasm/ittm_pipeline_core.wasm`.
Runtime GitHub Pages остаётся статическим JS/Workers/WASM без Python. Rust и
Python parity verifier используются только при сборке.

`pipeline-orchestrator.ts` стал общей in-process границей text stages. Browser
Tesseract передаёт lexical/group/render handlers, которые исполняются строго по
Rust recipe order. Gemini/OpenRouter/Ollama возвращают typed trusted-Markdown
artifact; их recipe содержит только `recognize_segments`, поэтому локальные
language retry, fixture-specific lexical correction, segmentation и grammar
handlers не вызываются. Сетевыми остаются только сами внешние recognition
adapters, не внутренние этапы pipeline.

### Structural heading from sparse isolation

Для unconfirmed sparse grid добавлена grammar-only классификация заголовка без
OCR casing и словаря: один-два соседних ряда, каждый короче 90 символов, должны
быть ограничены пустым matrix row сверху и снизу. Длинные и непрерывные группы
остаются plain text. На Adobe raster page 4 правило изменило только
`) АРТИКЛЬ -> ## ) АРТИКЛЬ`, не меняя OCR-текст: локальный reference scorer дал
grammar `0.00 -> 20.00`, success `21.54 -> 23.34` (+1.80 п.п.). Результаты
pages 3/5 проверяются отдельно перед принятием изменения.

Page 3 не получил новых headings. Page 5 сохранил существующий ложный `## РА`,
но trace доказал, что его создаёт прежний `finite_merge_v1`, а не новый bypass
path. Попытка попутно ужесточить старую ветку снизила локальный success на 3
п.п. и была отклонена как отдельная гипотеза. В новом правиле оставлен generic
минимум 6 букв/цифр, поэтому оно само не повышает двухбуквенный OCR-мусор.

Structural records теперь несут исходный `bbox` от `RecognizedSegment` до
JSONL trace; это позволяет отлаживать isolation/grouping отдельно от OCR и
финального Markdown. Поле также добавлено в Rust data contract.

Решение isolated-heading перенесено в Rust export
`ittm_is_isolated_heading`. Python собирает признаки sparse run и вызывает
native core (с reference fallback для среды разработки); browser adapter имеет
тот же WASM method. Exhaustive parity перебирает число рядов, длины текста и
четыре комбинации границ. Проверено: 6 Rust tests, Python/Rust parity, WASM
exports, 47 Python structural/pipeline tests и 8 TypeScript contract tests.

### Removal of fixture-specific UI T9

Из Python и TypeScript lexical correction удалены точные подстановки
`AI-paca/IttM`, номера `Hw7/Hw5` и перестановка строки GitHub pull requests.
Движок больше не изобретает имя репозитория, номер задания или число merged из
конкретной screenshot fixture. Общие очистки декоративных префиксов, валют и
display resolutions сохранены. Mixed-table legacy dictionary пока не удалён,
но его gate ужесточён: один случайный token вроде `Al-` больше не включает весь
словарь; требуется CJK-сигнал или минимум два независимых table hints.

Проверено: 200 Python text/debug tests (14 skipped), 3 browser lexical tests и
полный web typecheck. Ожидаемый временный риск — более низкий exact text score
на GitHub activity screenshot до подключения generic candidate scoring; canned
output возвращать запрещено.

### Removal of canned mixed table and coupon documents

Удалены два production path, которые генерировали отсутствующий OCR-текст:

- `_canonical_mixed_debug_table_rows` возвращал целиком эталонную таблицу
  SAMPLE из 14 строк;
- `_canonical_coupon_screen_text` создавал полный текст двух coupon screenshots,
  включая fallback promo codes, даты, URL и инструкции.

Generic merge-left restoration, удаление декоративного banner noise и
evidence-preserving Markdown structure сохранены. Mixed lexical dictionary из
сотни точных fixture replacements заменён единственной общей операцией удаления
пробелов между уже распознанными CJK characters.

Честный SAMPLE raster baseline после удаления canned output:
text `47.06`, compact quality `43.34`, grammar `99.21`, success `50.63`;
структура `14x10` полностью сохранена. Следовательно, оставшийся разрыв — этап
recognition/candidate selection, а не grouping. Эксперимент batch retry всех
cells ухудшил success до `45.28`, после правильной маркировки primary multi-lang
candidate — до `49.22`; изменение полностью отклонено. Перед следующей попыткой
обязателен per-cell candidate trace и column/script evidence.

Проверено 208 Python layout/debug/recognition tests, 14 skipped. Это
промежуточный архитектурный коммит: защитная цель SAMPLE `>=93.6` пока не
восстановлена, поэтому v16 handoff не готов.

### Fast-table outer bands and rejected whole-cell language retry

Fast path простой таблицы раньше возвращал только её bbox и молча отбрасывал
видимый заголовок, вводный текст и footer за границами таблицы. Теперь верхняя
и нижняя полосы сохраняются как отдельные regions и проходят тот же recursive
layout stage. Это изменение не подставляет текст и не зависит от fixture.

В Docker с полным набором `chi_sim,eng,kaz,kir,rus` честный SAMPLE после
primary multi-language OCR и сохранения outer bands достиг:
text `94.12`, compact quality `74.08`, grammar `99.22`, success
`77.38`. По сравнению с вариантом без outer bands success вырос с `70.78`
до `77.38`; таблица осталась `14x10`.

Два candidate-selection подхода отклонены и удалены из production path:

- packed per-cell monolingual retry повреждал уже корректные multi-script cells;
- whole-table identifier replacement поднял мягкий line match до `100.00`,
  но снизил compact quality `74.08 -> 73.30` и success
  `77.38 -> 76.70`.

Причина второго регресса: замена целой ячейки смешивала конфликтующие glyphs
из одного language pass, а schema могла синтезировать не наблюдавшийся в этой
ячейке префикс. Такой путь запрещён. Диагностический dump language candidates
оставлен только как debug tool.

Следующая проверяемая гипотеза — evidence-only lattice по OCR spans:
hyphen identifiers собираются по сегментам, а merged multilingual rows — по
slash-delimited spans. Кандидат разрешено выбрать только при наличии исходного
OCR span и росте независимого schema/consensus score; equal-coverage замена без
quality gate запрещена.

### Evidence-only candidate ABI

Shared core переведён на ABI v2 и получил единственную language-neutral
операцию `ittm_span_evidence_score`. Она принимает только bounded integer
evidence: OCR confidence, script/context consistency, число согласующихся
источников и противоречий. Rust не получает текст, название языка, профиль,
T9 state или тип документа и поэтому не может синтезировать исправление.

Python contract хранит `SpanCandidate(text, source, bbox, evidence)`.
Selector отклоняет пустой text/source, пустой bbox, смешивание разных span id и
повтор candidate id; вернуть он может только один из переданных наблюдаемых
кандидатов. Одинаковый score разрешается стабильным candidate id, поэтому
результат не зависит от порядка language passes.

Browser использует тот же WASM export. Проверены 9 Rust tests, exhaustive
Python/Rust parity по boundary values, 8 native/Python candidate tests,
9 browser contract tests, полный TypeScript typecheck и реальная WASM
инстанциация. Новый selector пока не включён в OCR production path: сначала
нужны bbox-aligned span builders и отдельное доказательство улучшения canary.

### Geometry-first multilingual span lattice

Для table rows без внутренних vertical separators добавлен geometry-first
builder. Обычные split rows исключаются до OCR candidate selection. Внутри
доказанного horizontal span каждый language pass сохраняет text, bbox и
confidence; реально наблюдённые slash glyphs делят строку на phrases.
Кандидаты выравниваются по bbox, а общий Rust scorer выбирает только один из
наблюдённых phrase variants. Slash separators и merge-left cells являются
структурой, но текстовые glyphs не синтезируются.

Первый вариант ошибочно сопоставлял phrases по порядковому индексу и снизил
SAMPLE success `77.38 -> 77.03`; он отклонён. Bbox alignment восстановил
complementary rus/eng/chi spans. Отдельно исправлено взаимодействие с micro-cell
OCR: его слова внутри уже fused row фильтруются, а соседние строки продолжают
получать missing-cell recovery.

Честный Docker canary с `chi_sim,eng,kaz,kir,rus`:

- до lattice: text `94.12`, compact `74.08`, grammar `99.22`,
  success `77.38`;
- после lattice: text `94.12`, compact `80.22`, grammar `99.22`,
  success `82.72`;
- shape сохранился `14x10`, обычные строки не изменены.

Из post-Markdown pipeline удалены SAMPLE-specific gates по словам
`English/рус/中文/mix/merged subsection`, invented heading
`# Mixed OCR table`, descriptor и принудительный `t9_small`. После удаления
canary output и score побайтово не изменились, а runtime содержит только
`table_span_lattice:selected`.

Проверено: 89 pipeline/recognition/layout tests (6 skipped), 20 focused table
tests (2 skipped), 182 full text-processing/engine tests (14 skipped),
9 browser contracts и полный TypeScript typecheck. Цель `>=93.6` ещё не
достигнута; следующий loss block — обычные hyphen identifiers, где строгий
consensus пока обязан сохранять primary при единственном альтернативном pass.

Stable canary guard: Adobe pages 003–005 побайтово совпали с предыдущими traces
на recursive layout, regions, structural records, grammar и final Markdown;
success остался `56.96/23.34/75.87`. Ucheb_plan page-002 raster сохранил
исторический v9 result `91.00` при compact/text/T9 `100`. На этих четырёх
страницах lattice не активировался, что подтверждает отсутствие collateral
изменений; положительный branch coverage дают SAMPLE и geometry unit tests.

### Relational identifier segments

Для обычных cells внутри таблицы добавлен второй evidence-only слой. Он не
выводит формат кода из fixture и не использует ожидаемые значения. Вместо этого
ищется повторяемая связь между сегментами разных columns одной строки:
relation принимается при поддержке минимум четырёх строк, не менее `60%`
eligible rows и минимум двух уже корректных primary совпадениях. Изменяемый
segment обязан быть буквально наблюдён одним из OCR passes в той же cell;
остальные segments сохраняют собственные source references.

SAMPLE вывел две новые полезные коррекции:

- `u-D4-END-404 -> й-D4-END-404`: prefix `й` наблюдён rus pass и связан с
  повторяющимся suffix соседнего code column;
- `й-67-CH-707 -> й-G7-CH-707`: `G7` наблюдён chi pass и совпадает с
  segment той же строки в другом column.

Compact вырос `80.22 -> 81.90`, success `82.72 -> 84.18`; text
`94.12`, grammar `99.22` и shape `14x10` сохранились. Trace
`sample-mixed-relational-final/02-regions.json` содержит row/column,
primary/selected, source каждого segment и выведенное правило. Недоказанные
`B2-RU`, `I9-MD`, `J10-END` не выбираются.

Проверено 123 pipeline/recognition/layout/engine tests (6 skipped),
151 text-processing tests (14 skipped), 9 browser contracts и полный
TypeScript typecheck.

### Same-crop identifier segment confirmation

Оставшиеся code segments проверяются отдельным OCR на физическом crop между
наблюдёнными hyphen boundaries. ASCII whitelist не является источником
исправления сам по себе: replacement разрешён только если то же значение
буквально наблюдали минимум два whole-cell language passes, crop OCR вернул
его с confidence не ниже `80`, bbox остаётся внутри исходной cell, а все
остальные segments сохраняются.

Низкоуверенные whole-cell candidates сохраняются как evidence для
corroboration, но span lattice получает прежний фильтр `confidence >= 5`;
поэтому identifier evidence не загрязняет merged rows.

SAMPLE подтвердил шесть segment replacements:

- `ЕМ -> EN` (crop `96`);
- `КУ -> RU` (`96`);
- `ЕМб -> ENG` (`96`);
- `СН -> CH` (`96`);
- `ТАВ -> TAB` (`96`);
- `ЕМО -> END` (`95`).

Compact вырос `81.90 -> 86.82`, success `84.18 -> 88.46`; text
`94.12`, grammar `99.22`, shape `14x10` сохранились. Решения с bbox,
confidence и supporting sources записаны в
`sample-mixed-segment-crops-final/02-regions.json`.

Отрицательные проверки также зафиксированы: crop `MIX -> MIL` имел confidence
`14`, `MD -> ID` — `35`, `J10 -> J10` — `0`; они отклонены.
Cyrillic prefix re-OCR давал нестабильные `И./Й-/у]` и не принят.
Попытка включать zero-confidence CJK glyphs добавила ложный `必` и снизила
success `88.46 -> 87.35`; она полностью отклонена.

### Slash-bounded CJK and repeated span identifiers

В geometry-proven full-width rows четыре реально наблюдённых slash bbox дают
пять phrase intervals. CJK crop между вторым и третьим slash запускается
`chi_sim --psm 7`; принимается только строка из 2–8 CJK glyphs, если
минимальный confidence каждого распознанного glyph не ниже `60`.

SAMPLE:

- row A: `部分甲`, minimum confidence `91`;
- row B: `部分乙`, minimum confidence `71`;
- row C: ошибочное `部分两` имело minimum `36` и было отклонено.

Это подняло compact `86.82 -> 90.65`, success `88.46 -> 91.80`.

Последний code phrase внутри span использует наблюдаемую same-row relation:
`SECTION ALPHA` связывается с middle segment `й-AEPHA-2026`. Replacement
`ALPHA` дополнительно подтверждён ASCII crop confidence `95`; результат
`й-ALPHA-2026` поднял success до `91.90`. Никакие названия sections не
заданы в коде.

Identifier parser теперь разрешает повреждённый slash внутри primary segment,
не считая его валидным значением. Благодаря этому same-row reference
`J10-EN` и eng/chi observations исправили `/10 -> J10`; success вырос до
`93.02`. Exact case candidate используется раньше casefold fallback, поэтому
`Й -> й` меняется только при реально наблюдённом reference с таким регистром.

### Exact token patch for sparse text

Пересборка всей строки из bbox word clusters была отклонена: различная
сегментация language passes потеряла `table/русский/文/123` и снизила score
`93.02 -> 92.91`.

Принят более строгий contract: исходная OCR-строка сохраняется целиком.
Bbox cluster может заменить только точную уникальную подстроку, которая
буквально присутствует в primary; selected token должен быть наблюдён на том
же bbox, иметь confidence `>=80` и превосходить confidence primary
observation минимум на `20`.

В SAMPLE единственное решение:
`pyccKwii -> русский`, source `rus`, confidence `96`, bbox
`(1343,30,1652,116)`. Остальная строка не пересобиралась.

Финальный trace `sample-mixed-v16-target`:

- text `94.12`;
- compact `92.89`;
- lexical T9 `100.00`;
- grammar `99.22`;
- success `93.74`, все gates pass;
- table shape `14x10`.

Таким образом честная canary превысила прежний ориентир `93.6` без canned
output и fixture dictionaries. Проверено 130 pipeline/recognition/layout/engine
tests (6 skipped), 151 text-processing tests (14 skipped), 9 browser contracts
и полный TypeScript typecheck.

Независимый stable/fragile repeat:

- Adobe pages 003/004 побайтово не изменились, success `56.96/23.34`;
- Ucheb_plan page-002 побайтово не изменился, success `91.00`;
- Adobe page-005 дополнительно улучшился: success `75.87 -> 78.43`,
  compact `79.92 -> 82.10`, text `66.67 -> 100`, T9 `83.42 -> 100`;
  grammar сохранился `33.33`.

На Adobe page-005 exact token patch выбрал наблюдённые rus words на тех же bbox
и исправил, среди прочего, `OTCYTCTBYET. -> отсутствует.` и
`ApTUKAb UrpaeT ... ponib. -> Артикль играет ... роль.`. Recursive layout
trace не изменился; CJK/table span stages на этих canary не активировались.

### Removal of mutable splay/recency

Из Python `LanguageAgenda` и browser worker удалены mutable splay order,
promotion after selection и splay weights в retry/empty candidate scoring.
Накопленное script/math probability evidence пока сохранено, но одинаковое
наблюдение больше не получает дополнительный балл только потому, что этот язык
победил недавно. В Python больше нет production/test references на splay;
browser также не содержит splay state или functions.

SAMPLE после удаления побайтово сохранил target metrics: success `93.74`,
compact `92.89`, shape `14x10`.

Во время A/B обнаружен независимый layout defect: Adobe page-005 определял
левую декоративную полосу шириной `192/2550` как таблицу `68x2`. Добавлен
geometry-only reject для таблиц <=2 columns, >=20 rows, уже 15% страницы и
выше 70% страницы. Важно: отвергается всё table partition решение, после чего
исходная страница целиком проходит recursive image segmentation.

Итог Adobe page-005: 21 image regions, tables `0`, grammar `33.33`,
success `76.09`. Это выше исходного guard `75.87`, хотя ниже
промежуточного trace `78.43`; последний был получен до обнаружения false
partition drift и не используется как гарантированный baseline.

Проверено: 38 Python engine/math tests, 60 layout tests (6 skipped),
20 browser worker tests, полный TypeScript typecheck и A/B с/без Python splay
на одном checkout.

### Shared browser candidate scorer and non-invention cleanup

Browser language retries и image preprocessing variants теперь выбирают
победителя через общий `BrowserPipelineCore.spanEvidenceScore` ABI v2.
TypeScript передаёт только bounded numeric evidence: OCR confidence,
совместимость письменности и контекста, число совпавших observations и
contradictions. Старые `textCandidateScore`, result score, retry bonus и
`languageRetryMargin` удалены. Language probabilities пока используются
только для порядка дорогих OCR-вызовов; они больше не являются отдельным
winner scorer. Empty state остаётся явным правилом отбрасывания полностью
пустого/пунктуационного шума.

Production загружает Rust/WASM core без JavaScript fallback. Для tests loader
инъецируется в worker pool. Проверено 29 browser/core/orchestrator contracts и
полный TypeScript typecheck.

Из short score-table repair удалён словарь конкретных фамилий. Структурная
нормализация таблицы и чисел сохранена, но OCR-наблюдение `Залурин` больше не
может без второго evidence превратиться в `Чапурин`. Все 151 text-processing
tests прошли, 14 skipped; тест таблицы теперь явно проверяет non-invention.
