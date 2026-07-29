# Этап 2: OCR блоков и проверяемое слияние доказательств

Этап 2 выполняется шестым в порядке чистого sparse-pipeline:

```text
3 → 1 → 6 → 4 → 5 → 2 → 7
```

На вход поступают готовые пересекающиеся блоки этапа 5. Для каждого блока
очередь создаёт пару `RAW → GAMMA` и отправляет её одному и тому же worker.
Разные блоки и независимые OCR lane выполняются параллельно.

Поддерживаемые lane:

- Tesseract TSV: `eng+chi_sim+rus`, PSM 4 или 6, восемь CPU worker;
- EasyOCR `en+ru`: отдельный постоянный процесс;
- EasyOCR `ch_sim+en`: отдельный постоянный процесс;
- GLM-OCR: один или два постоянных процесса, text-only результат.

EasyOCR и GLM-OCR запускаются точным Python из своего venv. Символические
ссылки venv не раскрываются через `resolve()`, иначе Python теряет `pyvenv.cfg`.
Обмен с дочерним процессом идёт по length-prefixed pipe-протоколу с request ID,
SHA-256 входного PNG, абсолютным timeout и строгой схемой результата. stdout
runtime перенаправляется в ограниченный stderr, поэтому progress bar не может
повредить протокол.

## Инварианты

- reference-текст запрещён при выборе OCR результата;
- сравнение удаляет только Unicode whitespace, остальные codepoint остаются
  точными;
- RAW и GAMMA одного блока всегда обрабатывает один worker последовательно;
- одинаковый `capability/context/transform` получает ровно один голос;
- разные тексты, bbox или confidence у реплик одной capability сохраняются как
  конфликт и принудительно дают `UNRESOLVED`;
- слово без единственного геометрического сегмента не отбрасывается: оно
  сохраняется в `unassigned-words.jsonl` и блокирует `COMPLETE`;
- OR/XOR считаются только над ID сегментов двух рассматриваемых блоков;
- text-only GLM не получает выдуманные bbox или confidence. Его block-text
  сохраняется для этапа 7 как `UNATTRIBUTABLE`;
- failed job не останавливает соседние lane или shard;
- лимиты количества jobs и байтов проверяются до построения массива jobs.

## Метрика

После завершения OCR, исключительно для отчёта, из reference и результата
удаляются все пробельные символы. `lost_characters` — точное расстояние
Левенштейна между оставшимися codepoint:

```text
accuracy = 100 × max(0, 1 - lost_characters / reference_length)
```

Если reference пуст, точное пустое совпадение получает 100%, а любой лишний
символ — 0%. Итоговая corpus-метрика считается из суммы loss и суммы символов
reference; среднее по страницам сохраняется отдельно как macro-диагностика.

Регистр, Unicode normalization и похожие символы разных алфавитов не
исправляются автоматически.

## Debug-прогон

Один CPU smoke:

```bash
python scripts/debug/debug_ocr_blocks.py \
  --input path/to/sample.png \
  --run-id stage2-smoke \
  --engines tesseract
```

Все lane на CUDA:

```bash
python scripts/debug/debug_ocr_blocks.py \
  --input debug/generated/stage1-line-owner-full1232-20260719 \
  --run-id stage2-full \
  --engines tesseract,easy-ru,easy-zh,glm \
  --easy-device cuda \
  --glm-device cuda:0 \
  --glm-workers 2
```

Подготовка страниц использует bounded thread-pool и идёт одновременно с OCR.
В обычном терминале можно явно выбрать `--prepare-executor process`.

Каждый item получает каталог `02-ocr` с полным `plan.json`, исходными
`segments.jsonl`, RAW/GAMMA PNG, результатом каждого job, выбранным текстом
каждого сегмента, block-text GLM, конфликтами, непривязанными словами, OR/XOR и
manifest. Перед первой записью writer заново строит fusion из этих сегментов,
queue и точных crop-байтов; forged digest/bbox/confidence/selection отвергаются.
Существующий run никогда не перезаписывается.

`accuracy_percent` относится к сегментному результату. Для страницы из одного
блока отчёт также содержит `best_block_accuracy_percent`; это post-hoc метрика,
а не утечка reference в выбор. Например, GLM может уже дать 100% block-text,
пока сегментная атрибуция честно остаётся `UNRESOLVED` до этапа 7.
