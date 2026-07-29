# Этап 7: сборка документа и полный debug-прогон v20

Этап 7 собирает документ только из доказательств этапов 1, 6, 5 и 2. Он не
перезапускает OCR, не исправляет распознанные символы и не получает эталонный
текст. Зафиксированный порядок чистого sparse-pipeline:

```text
3 → 1 → 6 → 4 → 5 → 2 → 7
```

В debug-runner этап 3 сначала проверяет bounded recursive controller. Этап 1
выравнивает страницу и строит lossless geometry, этап 6 восстанавливает
объекты, этап 4 проверяет необязательный gamma-кандидат, этап 5 строит
пересекающиеся блоки, этап 2 запускает OCR lanes и fusion, после чего этап 7
собирает текст, Markdown и provenance.

Для маленьких block-crop Tesseract получает детерминированный Lanczos-контекст
высотой не менее 320 px (не более 4× и 16 млн пикселей). Его TSV-boxes сразу
проецируются обратно в неизменную геометрию блока; коэффициент является частью
capability ID. Крупные блоки проходят без перекодирования и масштабирования.

Gamma-кандидат этапа 4 не заменяет RAW-страницу. Хеш точных RGB-пикселей RAW
связан с geometry этапа 1 и повторно проверяется перед crop и сборкой. Иначе
улучшатель мог бы незаметно изменить геометрию или съесть текст.

## Атрибуция и fail-closed поведение

Stage 7 сначала пытается использовать точную сегментную атрибуцию. Если OCR
даёт устойчивый текст только крупного блока, допустимо восстановление целого
объекта или уникальной группы сегментов через пересечения соседних блоков.
Неоднозначный, частичный или противоречивый результат остаётся `UNRESOLVED`;
ему не придумываются bbox, confidence или символы.

Каждый evidence slice хранит job, lane, capability, transform, SHA-256 входа,
объект/сегменты и точные смещения в candidate document. Text-only evidence без
доказуемой геометрии сохраняется как `UNATTRIBUTABLE`, а не приписывается
случайному объекту.

`DocumentArtifactWriter` перед записью повторно собирает результат из всех
переданных upstream-объектов. Каталог item публикуется через временный каталог
и atomic rename. В нём сохраняются:

```text
07-document/
├── candidate/document.txt
├── candidate/document.md
├── certified/                 # только для COMPLETE
├── records/
│   ├── evidence-slices.jsonl
│   ├── segments.jsonl
│   ├── structural-units.jsonl
│   └── objects.jsonl
├── objects/*/{candidate,certified}.{txt,md}
├── segments/*/{candidate,certified}.txt
├── diagnostics.txt
└── manifest.json
```

TSV-копия создаётся для каждого JSONL record set. Даже если OCR полностью
отказал, runner пытается опубликовать доступное Stage 7 evidence и только затем
помечает item как `failed`. Исключение не превращается в пустой успешный
результат.

## Точная метрика

Reference загружается строго после публикации Stage 7 artifact. Поэтому он
может использоваться только для post-hoc отчёта и не влияет на OCR/fusion.

Из reference и результата удаляются только символы, для которых Python
`str.isspace()` возвращает true. Регистр, normalization и похожие символы
разных алфавитов не меняются. `lost_characters` — точное расстояние
Левенштейна между оставшимися codepoint:

```text
accuracy = 100 × max(0, 1 - lost_characters / reference_characters)
```

Corpus accuracy является micro-метрикой: сначала суммируются loss и длины всех
reference, затем вычисляется процент. Среднее page accuracy сохраняется
отдельно и не подменяет micro результат.

## Параллельный corpus-runner

`debug_document_assembly.py` не пропускает все страницы через один
последовательный bottleneck:

- bounded process-pool параллельно готовит этапы 3/1/6/4/5;
- несколько постоянных OCR session одновременно обрабатывают страницы;
- внутри session независимые lanes и shard выполняют block jobs параллельно;
- окна `--prepare-window` и `--ocr-window` ограничивают память;
- весь corpus сначала пишется в скрытый partial-каталог и публикуется одним
  rename, существующий run-id не перезаписывается.

Пример для локального known-text corpus:

```bash
python3 scripts/debug/debug_document_assembly.py \
  --input debug/generated/stage1-known-strict-20260719-v2 \
  --output debug/tmp/document-corpus \
  --run-id stage7-known-24 \
  --limit 24 \
  --engines tesseract \
  --prepare-workers 4 \
  --ocr-page-workers 2
```

Для tracked sample reference задаётся отдельно:

```bash
python3 scripts/debug/debug_document_assembly.py \
  --input debug/fixtures/SAMPLE_4k.png \
  --reference-root debug/reference \
  --run-id stage7-sample \
  --engines tesseract
```

`summary.json`, `summary.tsv` и `summary.md` содержат status каждого item,
loss, micro accuracy, времена всех стадий и ссылки на evidence trees. Строгий
gate по умолчанию становится `GREEN` только одновременно при 100% покрытии
страниц reference-текстом, отсутствии execution failures, нуле
`UNRESOLVED` и micro accuracy не ниже `--minimum-accuracy-percent` (по
умолчанию 91.0%). Точный execution order и наличие Stage 7 artifacts также
остаются обязательными.

Для исследовательского прогона можно явно передать `--allow-missing-reference`
и/или `--allow-unresolved`. Такой ослабленный результат называется
`EXPLORATORY`, а не `GREEN`, поэтому его нельзя случайно принять за quality
gate. `--fail-on-unresolved` оставлен как явная запись строгого поведения по
умолчанию. Reference по-прежнему загружается только после публикации artifact
и не влияет на OCR или сборку документа.

## Единый v20 gate с уведомлением

```bash
scripts/debug/run-sparse-v20.sh
```

Wrapper запускает тесты в порядке 3/1/6/4/5/2/7, затем bounded known-text
прогон в безусловно строгом режиме с порогом 91.0%. После запуска он независимо
проверяет `gate_status=GREEN`, `gate_mode=strict`, 100% scoring coverage,
нулевые failures и unresolved, micro accuracy и точный execution order. Любая
команда проверяется по exit code. Успех публикуется только после всех GREEN
gates; при успехе и ошибке вызывается `notify-send`.

Это намеренно не полный 1 232-image corpus. По умолчанию используется максимум
24 known-text PNG. Размер sample можно явно изменить через `V20_LIMIT`, вход —
через `V20_INPUT`, число параллельных OCR sessions — через
`V20_OCR_PAGE_WORKERS`, lanes — через `V20_ENGINES`.

Полный воспроизводимый прогон всех 1 232 PNG запускается явно:

```bash
RUN_ID=v20-full1232-$(date +%Y%m%d-%H%M%S) \
V20_INPUT=debug/generated/stage1-line-owner-full1232-20260719 \
V20_LIMIT=1232 \
V20_PREPARE_WORKERS=4 \
V20_OCR_PAGE_WORKERS=2 \
V20_TESSERACT_WORKERS=4 \
V20_ENGINES=tesseract \
scripts/debug/run-sparse-v20.sh
```

Wrapper пишет таблицу каждого gate в `debug/tmp/v20-gates/<RUN_ID>/gates.tsv`,
полный Stage 7 отчёт — в `debug/tmp/document-corpus/<RUN_ID>-sample/` и сам
отправляет desktop-уведомление после окончательного GREEN или RED.
