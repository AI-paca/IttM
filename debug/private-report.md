# Локальный отчет Hw5

Дата: 19 июня 2026 года.
База: `origin/Hw5` at `91b83233235f04c593c6b3cecdf7852b3cfcb802`.

## Что изменено после remote commit

До очистки поверх remote base было 39 локальных коммитов с промежуточными
OCR-попытками и исправлениями результатов. История пересобрана по
ответственности:

1. extraction/upload/stream/PDF assets вместе с их regression tests;
2. OCR runtime для сканов, таблиц и projector photos вместе с regression tests;
3. локальные LLM OCR runners вместе с их tests;
4. debug corpus, runner-ы, per-method matrix и debug-tool tests;
5. документация и ее CI contract одним docs-коммитом.

`debug` не называется автоматическими тестами: это локальная тяжелая
отладочная среда. Проверки ее скриптов находятся отдельно в `ocr/tests/`.

Отдельный перенос в `engine` не сделан. Локальная `engine` не менялась и
совпадает с `origin/engine` (`9481d16`). Новый OCR runtime остается в итоговой
`Hw5`, чтобы `Hw6` и `Hw7` получили одну базу без второго набора
cherry-pick/rebase-хэшей.

## Исправления OCR

- Для больших landscape/A3-подобных сеток добавлены overlapping tiles,
  горизонтальные полосы, удаление линий таблицы и повторное распознавание
  мелкого текста.
- Wide sparse pages получают отдельный cover fallback и подходящие PSM.
- Projector photos получают отдельную геометрию dewarp, выбор text/diagram
  slide, дополнительные borders и защиту от повторного transform.
- Long screenshots ограничиваются до spatial layout.
- Табличный Markdown сохраняет пустые placeholder-ячейки.
- Browser Tesseract получил те же классы fallback. Node benchmark теперь
  выполняет Canvas preprocessing через `@napi-rs/canvas`, а не пропускает его.
- Произвольные production flag overrides не открыты наружу; каталог флагов
  используется для профилей, debug-отчета и документационного контракта.

## Проверка учебных планов как изображений

Четыре учебных плана отрендерены при 300 DPI в PNG и JPEG. Каждая страница
сравнивается отдельно; PDF text layer не участвует. Все строки Tesseract,
EasyOCR и browser Tesseract сохранены в матрице.

Минимум среди PNG и JPEG для каждого плана:

| План | Tesseract | EasyOCR | Browser |
| --- | ---: | ---: | ---: |
| `000041301...pdf` | 90.21% | 90.21% | 94.47% |
| `09.03.03_05...pdf` | 91.04% | 97.01% | 91.67% |
| `УП2022...pdf` | 92.00% | 92.00% | 95.45% |
| `Ucheb_plan_020302...pdf` | 97.96% | 99.50% | 99.00% |

Итоговая `debug/result.csv` содержит 48 файловых строк и 138 фактических
method results. Failures и значения `0` отсутствуют. Шесть `n/a` относятся
только к browser/PDF, поскольку browser runner в этой матрице принимает
растровые входы, а не исходный PDF.

Non-plan regression rows также проходят свои пороги:

- `photo_6...jpg`: Tesseract 60%, EasyOCR 50%, browser 55%, threshold 50%;
- `photo_10...jpg`: 100%, 92.31%, 100%;
- `SAMPLE_4k.png`: 100% всеми тремя методами;
- mixed SAMPLE PDF: 100% backend Tesseract/EasyOCR;
- Adobe scan PDF: 92.86% Tesseract, 95.24% EasyOCR.

Цена correctness: самые тяжелые dense-grid страницы занимают примерно
10–12 минут на метод; browser достигал около 1.2 GiB RSS. Эти прогоны остаются
локальным/scheduled debug tier и не добавляются в быстрый PR gate.

## Структура debug

- `debug/fixtures/` — входные изображения/PDF;
- `debug/reference/` — ручные Markdown-эталоны;
- `debug/tmp/` — игнорируемые промежуточные результаты;
- `debug/result.csv` — качество каждого метода, profile и flags;
- `debug/time.csv` — время каждого метода, profile и flags.

`fixtures` расположена выше `reference` по алфавиту. Старой вложенности
`expected/expected-*` нет. В Git остаются только два SAMPLE media,
Markdown-reference файлы и `.gitkeep`; остальные реальные fixtures физически
остаются в `debug/fixtures/`, но игнорируются.

## Проверки

- `npm run typecheck` — passed.
- `npm test` — 182 passed.
- Targeted Python OCR/debug suite — 81 passed, 1 skipped.
  Skip относится к optional XLSX test при отсутствии `openpyxl`.
- `scripts/debug/debug_quality_gate.py` — passed.
- `git diff --check`, Python compile и shell syntax — passed.

Документация готовилась параллельно в worktree
`/home/alpaca/GitHub/IttM-Hw5-docs-edit` на ветке
`docs/hw5-report-edit`, затем ее единственный публичный commit был
перенесен поверх чистой Hw5. Приватные `.zoo/.review-from-llm copy/`
остались незакоммиченными.

## Приватные PR-файлы

- `pr.md` — полный локальный аудит всего diff.
- `pr-sample.md` — короткий вариант по стилю прошлых PR проекта.

Оба файла исключены через локальный `.git/info/exclude` и не попадут в Git.
