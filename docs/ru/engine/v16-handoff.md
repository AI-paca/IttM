# v16 handoff: atomic evidence pipeline

## Ветка и commits

Ветка: `refactor/atomic-recursive-pipeline`.

Последний проверенный commit с metric-reaching OCR изменениями:
`710046b resolve fragile OCR spans from aligned evidence`.

Ключевая последовательность:

- `4173305` — atomic recursive layout stages;
- `9518308` — typed Python pipeline core;
- `959b12e` / `2bbc487` — Rust ABI, WASM и native adapter;
- `d2ad174` / `ea72ad5` — shared browser recipe и capability isolation;
- `9005d83` / `43cb187` — удаление fixture substitutions и canned documents;
- `37c5302` — сохранение outer bands вокруг fast-path table;
- `ffbb067` — evidence-only span scoring ABI v2;
- `b3941a9` — geometry-first multilingual table spans;
- `edd18ab` — relational identifier segments;
- `4d6d6c5` — same-crop identifier confirmation;
- `710046b` — slash-bounded CJK, repeated span IDs и exact token patch.

Пользовательский untracked файл `scripts/debug/compare_v1_v15.py` не входит
ни в один commit и не изменялся.

## Где изменён pipeline

- `pipeline-core/src/candidates.rs` — единственный Rust scorer numeric evidence;
- `ocr/app/pipeline_core/candidates.py` — выбор только наблюдённого candidate;
- `ocr/app/layout/stages.py` — recursive outer bands;
- `ocr/app/recognition/span_lattice.py` — horizontal spans, slash phrases,
  CJK crops и repeated span identifiers;
- `ocr/app/recognition/identifier_lattice.py` — межколоночные segment relations;
- `ocr/app/recognition/segment_crop_lattice.py` — same-physical-crop confirmation;
- `ocr/app/recognition/text_token_lattice.py` — exact primary substring patch;
- `ocr/app/services/convert_service.py` — orchestration и typed trace metadata;
- `web/src/ocr/pipeline-core.ts` — тот же WASM ABI v2.

Ни один lattice stage не получает reference Markdown, fixture filename или
ожидаемое значение. Synthetic text запрещён: emitted token/segment обязан
иметь source OCR observation и bbox; synthetic разрешены только Markdown и
merge/slash structural separators.

## Canary, который должен воспроизвестись

Fixture:
`debug/tmp/fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf.raster.png`.

Reference:
`debug/reference/SAMPLE_mixed_ru_en_zh_table_image.pdf.raster.png.md`.

Проверенный Docker trace:
`debug/artifacts/v16-stage-traces/sample-mixed-v16-target`.

Ожидаемые локальные показатели:

| Metric | Before evidence stages | v16 target trace |
| --- | ---: | ---: |
| text | 94.12 | 94.12 |
| compact | 74.08 | 92.89 |
| lexical T9 | 100.00 | 100.00 |
| grammar | 99.22 | 99.22 |
| success | 77.38 | 93.74 |
| table shape | 14x10 | 14x10 |

Все gates в target trace имеют `pass`. Прежний ориентир `93.6` превышен.

Короткий воспроизводимый запуск:

```bash
docker run --rm --entrypoint python \
  -v "$PWD:/workspace" \
  -w /workspace \
  -e PYTHONPATH=/workspace/ocr:/opt/ittm-python-packages \
  -e ITTM_PIPELINE_CORE_LIB=/workspace/pipeline-core/target/release/libittm_pipeline_core.so \
  ittm-ocr-ci \
  scripts/debug/dump-recursive-pipeline-trace.py \
  debug/tmp/fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf.raster.png \
  --output-dir debug/artifacts/v16-stage-traces/sample-mixed-v16-repeat \
  --engine tesseract \
  --profile backend_tesseract_standard
```

Скоринг:

```bash
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "scripts/debug")
from debug_report import scored_expected_match

root = Path("debug/artifacts/v16-stage-traces/sample-mixed-v16-repeat")
expected = Path(
    "debug/reference/SAMPLE_mixed_ru_en_zh_table_image.pdf.raster.png.md"
).read_text()
actual = (root / "05-final-markdown.md").read_text()
print(scored_expected_match(actual, expected))
PY
```

## Где смотреть причину каждого решения

`02-regions.json`, поля `meta`:

- `span_candidate_decisions`;
- `cjk_span_crop_decisions`;
- `repeated_span_identifier_decisions`;
- `identifier_candidate_decisions`;
- `identifier_segment_crop_decisions`;
- `text_token_lattice_decisions`.

Ожидаемые runtime flags:

- `table_span_lattice:selected`;
- `table_cjk_span_crops:selected`;
- `table_repeated_span_identifier_crops:selected`;
- `table_identifier_relations:selected`;
- `table_identifier_segment_crops:selected`;
- `text_token_lattice:selected`.

Если score отличается, первым делом сравнивать `01-aligned.png`,
`02-recursive-grid-trace.json`, затем перечисленные decisions. Это разделяет
alignment, grouping, OCR candidates и grammar вместо проверки только final MD.

## Stable/fragile guards

- Adobe page-003: success `56.96`, output без изменений;
- Adobe page-004: `23.34`, output без изменений;
- Adobe page-005: исходный guard `75.87`, текущий deterministic trace
  `76.09`; ложная narrow-table partition удалена;
- Ucheb_plan page-002 raster: `91.00`, output без изменений.

Повторные traces:
`debug/artifacts/v16-stage-traces/*-token-lattice-regression`.

## Проверки перед commit

- 130 pipeline/recognition/layout/engine tests passed, 6 skipped;
- 151 text-processing tests passed, 14 skipped;
- 29 browser worker/core/orchestrator contracts passed;
- TypeScript typecheck passed;
- Rust tests, exhaustive Python/Rust parity и WASM instantiation прошли для ABI v2.

## Ожидания от полного v16 benchmark

Это ожидания, а не подмена реального полного прогона:

- SAMPLE mixed raster success не ниже `93.6`, shape `14x10`;
- Ucheb_plan page-002 raster остаётся около `91.0`;
- Adobe page-005 не ниже предыдущих `75.87`, текущий локальный trace
  `76.09`;
- Adobe pages 003/004 не должны изменить OCR/layout output;
- native text-layer PDFs не должны проходить table lattices без geometry spans;
- trusted API/VLM outputs не должны включать local T9, lexical или grammar stages.

## Незавершённый legacy audit

Успешный canary не означает, что весь legacy уже удалён:

- mutable splay/recency удалён из Python и browser; browser language/image
  winner selection перенесён в shared Rust/WASM scorer, probabilities остались
  только планировщиком порядка OCR-вызовов;
- curriculum canonical labels и часть Amazon domain repair остаются
  evidence-risk paths;
- словарь конкретных фамилий в short score-table repair удалён; такие ячейки
  теперь сохраняют наблюдённый OCR token до появления независимого evidence;
- полные native/raster/images v16 averages может подтвердить только новый
  benchmark run.

До разбора этих пунктов и пользовательского v16 run цель полного переписывания
остаётся активной.
