# Debug

[Документация](./README.md) | [Движок](./engine/README.md) | [Тестирование](./testing.md)

`debug/` - локальная рабочая зона для воспроизведения реальных OCR-проблем.
Это не CI tier: полезный случай должен затем стать generated fixture с явным
oracle.

## Структура

```text
debug/
├─ README.md
├─ .env.sample
├─ fixtures/                  # tracked два SAMPLE; реальные входы ignored
├─ reference/                 # tracked ручные .md эталоны
├─ result.csv                 # итоговое качество по каждому методу
├─ time.csv                   # итоговое время по каждому методу
├─ private-report.md
└─ tmp/
   ├─ tesseract/
   ├─ easyocr/
   ├─ browser-tesseract/
   ├─ api-ollama/
   ├─ api-openrouter/
   └─ api-gemini/
```

`debug/` является самодостаточным ручным corpus: входной файл
`fixtures/name.ext` связан с ручным эталоном `reference/name.ext.md`.
В git остаются два SAMPLE-входа и все ручные `.md`; реальные fixtures
сохраняются локально, но игнорируются.
`tmp/` целиком игнорируется и не имеет `.gitkeep`: `scripts/debug/debug-all.sh`
создает его и вложенные каталоги движков при первом запуске в новом worktree.
SAMPLE никогда не записываются в `tmp/`.

## Одна Команда

Полный локальный прогон по умолчанию:

```bash
scripts/debug/debug-all.sh
```

По умолчанию запускаются `tesseract,easyocr,browser-tesseract`; API-движки не
запускаются. Fixtures берутся из `debug/fixtures/`, а ручные
эталоны - из `debug/reference/`. `testtables/` остается только
legacy fallback для старых локальных прогонов. Adobe Scan автоматически
ограничивается первыми 5 страницами, потому что manual expected покрывает
только их.
Выбранные PDF также автоматически превращаются в PNG и JPEG image fixtures, так
что в итоговых CSV появляются отдельные строки `*.pdf.raster.png` и
`*.pdf.raster.jpg`.

Результат:

```text
debug/result.csv
debug/time.csv
debug/tmp/<engine>/
```

PDF raster-строки являются acceptance-проверкой image path. Только debug-runner
заранее рендерит учебные планы в отдельные PNG/JPEG при 300 DPI и сравнивает
эти изображения без PDF text layer. Release/API default остается
`pdf_mode=auto`: пригодный текстовый PDF не переводится в bitmap, а скан
автоматически уходит в OCR. Для явного API-прогона того же сценария доступен
`pdf_mode=raster`. CSV записываются до того, как quality gate сообщает
failures.

Tracked sample:

- `fixtures/SAMPLE_4k.png` - 3840x2160 слово SAMPLE касается
  границ изображения; default Tesseract debug recognition должен оставаться
  выше 90%.
- `fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf` - image-only PDF
  с hard 10x14 таблицей: русский, English, 中文, цифры, mixed ID с `й`,
  слитые subsection rows и проверка Markdown placeholder cells.

Обычные итоговые файлы только CSV. XLSX используется только отдельным скриптом
подбора флагов.

Извлеченные таблицы сохраняются отдельно как один
`debug/tmp/tables/<method>/<fixture>.tables.md` на результат. Все найденные
table blocks объединяются внутри файла, поэтому одна сложная страница не
создает сотни мелких артефактов. Ячейки разделены `|`, пустые
placeholder-ячейки не схлопываются. CSV для самих таблиц не создается.

## Один Файл

```bash
scripts/debug/debug-all.sh --fixture 'image (7).png'
```

`--fixture` принимает glob и может повторяться.

Ограничить страницы PDF для backend:

```bash
scripts/debug/debug-all.sh --fixture 'Adobe Scan Oct 26, 2022 (1).pdf' --max-pages 5
```

Для смешанного набора используйте правило на конкретный файл:

```bash
scripts/debug/debug-all.sh --fixture-max-pages 'Adobe Scan Oct 26, 2022 (1).pdf=5'
```

Отключить raster-строки для быстрого PDF-only прогона:

```bash
scripts/debug/debug-all.sh --fixture '*.pdf' --no-pdf-raster
```

Изменить форматы или лимит страниц для raster-строк:

```bash
scripts/debug/debug-all.sh \
  --fixture '*.pdf' \
  --pdf-raster-formats png,jpg \
  --pdf-raster-max-pages 5
```

## Движки

Все non-API движки:

```bash
scripts/debug/debug-all.sh --engines tesseract,easyocr,browser-tesseract
```

Один backend-движок:

```bash
scripts/debug/debug-all.sh --engines tesseract
```

Только backend без browser:

```bash
scripts/debug/debug-all.sh --engines tesseract,easyocr
```

Только browser для image fixtures:

```bash
scripts/debug/debug-all.sh --engines browser-tesseract --fixture '*.png'
```

API-движки пока являются каркасом. Выбор `api-ollama`, `api-openrouter` или
`api-gemini` завершается понятной ошибкой, а не фальшивым результатом.

## Флаги

По умолчанию backend использует автоматический профиль для каждого engine.

Один профиль для всех backend-движков:

```bash
scripts/debug/debug-all.sh \
  --engines tesseract,easyocr \
  --pipeline-profile backend_plain_text
```

Профиль только для одного backend-движка:

```bash
scripts/debug/debug-all.sh \
  --engine-profile tesseract=backend_tesseract_standard \
  --engine-profile easyocr=backend_easyocr_table
```

Browser-профиль:

```bash
scripts/debug/debug-all.sh --browser-profile browser_tesseract_dewarp
```

Browser benchmark использует Node Canvas shim и выполняет тот же image
preprocessing, что UI: resize/dewarp, edge-word fallback, sparse cover и
dense-grid multi-pass. Для плотных учебных планов это тяжёлый отладочный
прогон; timeout по умолчанию равен 900 секундам.

## Матрицы

`result.csv` больше не выбирает лучший метод. В нем есть колонки:

```text
file,threshold,tesseract %,easyocr %,browser-tesseract %,tesseract gate,...
```

`gate` считается отдельно для каждого метода. Для PDF и всех созданных из PDF
300 DPI raster-строк порог равен 90%. Базовый порог остальных изображений -
87%. Специальные пороги для заведомо тяжелых неучебных фото/сканов:

- `Adobe Scan Oct 26, 2022 (1).pdf`: 70%;
- `photo_6_2026-05-12_22-26-36.jpg`: 50%.

`n/a` разрешен для неподдерживаемых пар метод/файл, например browser/PDF.

## Подбор Флагов

```bash
python3 scripts/debug/debug_flag_sweep.py \
  'debug/fixtures/image (7).png' \
  --output debug/tmp/flag-sweep-image7.csv \
  --xlsx-output debug/tmp/flag-sweep-image7.xlsx \
  --scale 1 --scale 2 --scale 3 \
  --preprocess rgb --preprocess autocontrast \
  --psm 3 --psm 4 --psm 6 --psm 11 --psm 12
```

CSV содержит все проверенные варианты. XLSX нужен только для подбора флагов:
лучший вариант для каждого файла подсвечивается желтым.

## PDF Как Картинки

Обычный `scripts/debug/debug-all.sh` уже добавляет выбранные PDF как PNG и JPEG
image-строки. Низкоуровневый probe нужен только если надо создать такие
fixtures без полного matrix-прогона:

```bash
scripts/debug/debug_pdf_image_probe.py \
  'debug/fixtures/09.03.03_05(ИУ1).pdf' \
  --max-pages 5 \
  --format png,jpg
scripts/debug/run-debug.sh \
  --fixtures debug/tmp/pdf-image-fixtures \
  --expected-root debug/tmp/pdf-image-reference \
  --output debug/tmp/pdf-image-probe-run \
  --engines tesseract \
  --fixture '09.03.03_05(ИУ1).pdf.raster.png' \
  --timeout 420 \
  --gpu auto
```

Сгенерированные PNG/JPEG и временный expected остаются в `debug/tmp/`. Expected
для probe ограничивается теми же первыми страницами, которые были отрендерены.
Это отдельная оценка bitmap-входа, а не замена default PDF parser.

## API Env

Для будущих API-движков:

```bash
cp debug/.env.sample debug/.env
```

В sample есть локальная Ollama:

```text
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=
```

`debug/.env` локальный и не коммитится.

## Expected

`debug/fixtures/<file>` - входной файл, а
`debug/reference/<file>.md` должен быть ручным эталоном или
явно проверенной разметкой. Нельзя заполнять expected выводом одного OCR engine
и выдавать это за truth.

Процент в `result.csv` - recall проверенных строк из manual expected. Он не
заменяет quality oracle и не говорит о стабильности таблиц сам по себе.
