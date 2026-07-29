# OCR pipeline: sparse tables, reviewer T9, hardcode cleanup

### Движок

- Добавлены модули распознавания: вероятностная языковая agenda для retry OCR, кандидаты и батчи для ре-распознавания, функциональная метрика качества (token/pair/digit recall), блоки на перепроверку, сегментация областей.
- Исправлена регрессия sparse empty: `EMPTY_SLOT_CODE` больше не удаляет ячейку из OCR-очереди без повторной пиксельной проверки, поэтому тонкие цифры/математика в "пустых" клетках доходят до OCR.
- Ячейки таблиц теперь проверяются на цветовой foreground, а не только на grayscale: chromatic/flat-luma символы не помечаются пустыми и перед OCR получают отдельный бинаризованный crop.
- T9 retry стал учитывать mixed-script OCR noise на уровне слов/ячеек: латинские таблицы с кириллическими вставками уходят в перебор языка, но сбалансированные RU+EN строки не штрафуются.
- T9 agenda теперь хранит `empty` как отдельное reviewer-state в probability/splay порядке, а одиночные числовые ячейки учат `equ`: пустые/шумные OCR-кандидаты могут проиграть пустой ячейке, но `empty` не отправляется в Tesseract как язык и не скрывает одиночные цифры.
- Result-grid грамматика стала устойчивее к OCR-искажениям rating/deal полей и хвостам price/bought внутри product cell.
- Sparse markdown больше не считает одиночный текстовый поток доказанной таблицей: одноколоночный текст уходит в plain/fallback path, а lossy finite-merge откатывается до OCR parts вместо потери строк.
- Финальный markdown formatter удаляет повторные OCR-блоки без трогания markdown-таблиц; это снимает compact-regression от near-duplicate full-page/recursive-grid fallback.
- Добавлены модули форматирования: лексическая коррекция вывода, структурная грамматика markdown, журнал структурных правок.
- Добавлены модули layout: рекурсивная сетка для таблиц, разреженное кодирование пустых ячеек, слоты таблиц, авто-выбор layout.
- Вертикальный chunking — нарезание таблиц по вертикали.
- Слои предобработки и layout, выбираемые автоматически через auto resolver: browser dense-grid (плотная сетка для таблиц), sparse-cover (разреженное покрытие), projected document dewarp (выправление перспективы).
- Tesseract engine: поддержка новых pipeline flags, подготовка к probabilistic retry.

### Архитектура

- Browser-side text reviewer pipeline на базе `HuggingFaceTB/SmolLM2-135M-Instruct`.
- Browser T9 OCR для слабых сегментов пробует дополнительные image variants (invert/contrast/Otsu) и выбирает результат по text/T9 score + confidence; сильный первичный OCR не гоняется по вариантам.
- Browser tiny-review profile нормализует связанные флаги: `lexicalCorrection=t9_small` включает `ocrLanguageRetry=t9_small` и table slots, worker cache разделяет разные reviewer/table настройки.
- Browser default `browser_tesseract_dewarp` теперь включает `t9_small` и `recursive_gaps_v1`; `ell/equ` не добавляются неявно без traineddata, а остаются явным opt-in через `browser_tesseract_greek_math`.
- Browser table slots восстанавливают повторяющиеся product-card grids в `Field | Result N` без хардкода конкретного сайта, сохраняют левый фильтр/контекст как plain-text префикс и не превращают длинный prose-документ в узкую псевдо-таблицу.
- Native PDF text-layer path изолирован от OCR-рекурсии в диагностике: для полностью читаемых PDF теперь видно `engine=pdf_text_layer`, `layout_steps=pdf_text_layer_fixed_width`, `chunks=0`, без misleading `recursive_grid`/T9 OCR flags.
- Browser dense-grid — плотная сетка для таблиц в браузере.
- Контекстуальный markdown — сборка markdown из layout + OCR на клиенте.
- Выравнивание строк — alignment детектированных строк таблиц.
- Layout stages и selectors — расширена логика выбора pipeline.
- Проверка лимитов и объёма PDF до загрузки пикселей — защита от OOM.
- NDJSON streaming: клиент получает страницы по мере готовности, а не после обработки всего документа.
- Новые UI-компоненты: диагностика качества markdown, прогресс по страницам.

### Debug

Добавлено полное рабочее пространство для отладки OCR: прогон по всем engine/fixture комбинациям, параллельный Tesseract + EasyOCR + browser-tesseract, переоценка markdown-выводов без rerun OCR, сборка эталонных фикстур, quality gate. Полное описание — в `docs/ru/debug.md`.

Debug backend теперь запускается с `PYTHONPATH=/app:/opt/ittm-python-packages`, поэтому Docker benchmark проверяет текущий worktree из bind-mount, а не старый пакет из volume.

Обновлены native PDF reference markdown для нескольких учебных планов, а не одного файла: `000041301_UchebPlan_sign000029629.pdf`, `09.03.03_05(ИУ1).pdf`, `Ucheb_plan_020302-2022-O-PP-4y00m-02.pdf`. Старые refs для первых двух были плоским текстом с form-feed и не проверяли таблицы в Web UI; новые refs соответствуют `pdf_text_layer_fixed_width` markdown и дают `100%` text/compact/T9/grammar на текущем native-output.

Phase2 sparse-regression canaries после guard:
`photo_10_2026-05-12_22-26-36.jpg` (`tesseract`) вернулся с `65.94%` к `96.98%` success за счет удаления duplicate fallback blocks; `Screenshot_2026-06-06-13-43-07-99.jpg` (`tesseract`) держит v13-уровень `32.03%` вместо v14 `19.22%`. `photo_6_2026-05-12_22-26-36.jpg` остается отдельным projector/diagram OCR gap (`43.07%`) и требует следующей правки candidate selection, а не reference/table hardcode.

Локальный canary по `photo_2026-06-26_19-56-47.jpg` относительно v12: `tesseract` поднялся `46.01% -> 61.39%` success и `39.25% -> 56.82%` compact quality; `easyocr` поднялся `34.07% -> 49.10%` success и `25.53% -> 42.69%` compact quality. `lexical t9` на этом срезе остается `100%`, поэтому это рост T9-enabled OCR quality, а не отдельной T9-колонки. Browser canary по root `Image.png` после weak-segment variants поднялся `39.84% -> 50.08%`; `doc_docs_ru_architecture_current_flags.png` остался около v12 (`80.95% -> 80.48%` для `tesseract`, `80.84% -> 80.84%` для `easyocr`). До целевых `90%+` остаются нерешенные gaps в OCR text fidelity и восстановлении логических строк/deal/badge/product fields.

<details>
<summary>Пример отладки по всем бенчмаркам</summary>

```bash
scripts/debug/debug-all.sh \
  --engines tesseract,easyocr,browser-tesseract \
  --fixtures debug/fixtures \
  --expected-root debug/reference \
  --fixture '*.pdf' \
  --no-pdf-raster \
  --max-pages 5 \
  --gpu auto \
  --timeout 1200 \
  --tmp-root debug/tmp/full-pdf-native \
  --output-root debug/artifacts/full-pdf-native

scripts/debug/debug-all.sh \
  --engines tesseract,easyocr,browser-tesseract \
  --fixtures debug/fixtures \
  --expected-root debug/reference \
  --fixture '*.pdf' \
  --pdf-raster-only \
  --pdf-raster-formats png \
  --pdf-raster-max-pages 5 \
  --pdf-raster-dpi 300 \
  --gpu auto \
  --timeout 1200 \
  --tmp-root debug/tmp/full-pdf-as-png \
  --output-root debug/artifacts/full-pdf-as-png
```

</details>

### CI

- Добавлен workflow `ocr-quality.yml` — OCR quality tier в CI.
- Добавлен workflow `container-images.yml` — сборка Docker-образов.
- Добавлен workflow `sca.yml` — Software Composition Analysis.
