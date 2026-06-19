# Тестирование

[English](../en/testing.md) | [Документация](./README.md)

Документ описывает test platform в её релизном виде. Цель — чтобы `curl` и Web UI
проверялись одним и тем же набором tiers и одной и той же oracle-логикой, а не
разрозненными ручными прогонками.

## Tiers

| Tier                 | Что проверяет                                                                       | Команда                                                     | Владелец             | Время         |
| -------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------- | -------------------- | ------------- |
| Contract             | границы контракта: lifecycle, worker protocol, layout contracts, allowlist профилей | `npm run test:contract`                                     | gateway + web        | секунды       |
| Smoke                | маршруты `/api/*`, nginx/gateway/OCR glue                                           | `npm run test:smoke`                                        | gateway              | минуты        |
| Unit / format / lint | стиль и типы                                                                        | `npm run format:check && npm run lint && npm run typecheck` | web + gateway + edge | секунды       |
| Resource             | память, время, граничные размеры PDF/images, soak                                   | `npm run test:resources`                                    | ocr/app              | десятки минут |
| OCR quality          | generated fixtures через backend Tesseract/EasyOCR                                  | `npm run test:ocr:quality`                                  | ocr/app              | десятки минут |
| OCR browser quality  | browser Tesseract.js                                                                | `npm run test:ocr:browser`                                  | web                  | минуты        |
| Pages                | GitHub Pages verifier с `/IttM/` base path                                          | `npm run build:pages && npm run test:pages`                 | web                  | минуты        |
| Compose              | docker compose end-to-end                                                           | `npm run test:compose`                                      | infra                | минуты        |
| Python lint          | flake8, Black, Ruff, pytest в OCR test image                                        | см. ниже                                                    | ocr/app              | минуты        |

## Что сейчас есть в коде

- `web/src/ocr/api-client.test.ts`, `gateway/src/core/routes.test.ts`,
  `gateway/src/core/handle.test.ts`, `gateway/src/core/node-adapter.test.ts`,
  `gateway/src/core/compose-contract.test.ts`,
  `gateway/src/tasks/task-service.test.ts`,
  `gateway/src/tasks/process-worker.test.ts`,
  `gateway/src/tasks/input-storage.test.ts`,
  `gateway/src/services/staticFiles.test.ts`,
  `gateway/src/cli/run.test.ts`,
  `gateway/src/cli/extraction-client.test.ts`.
- `ocr/tests/test_main.py`, `ocr/tests/test_upload_processing.py`,
  `ocr/tests/test_layout_pipeline_contracts.py`,
  `ocr/tests/test_layout_geometry.py`, `ocr/tests/test_layout_stages.py`,
  `ocr/tests/test_preprocessing.py`, `ocr/tests/test_ocr_quality.py`,
  `ocr/tests/test_quality_metrics.py`,
  `ocr/tests/test_table_fixtures.py`, `ocr/tests/test_text_processing.py`,
  `ocr/tests/test_auto_engine.py`, `ocr/tests/test_visual_mutations.py`,
  `ocr/tests/test_generated_fixture_registry.py`,
  `ocr/tests/test_generated_media_matrix.py`,
  `ocr/tests/test_document_templates.py`,
  `ocr/tests/test_pdf_progress.py`.
- Generated fixture registry в `ocr/tests/generated_media.py` уже даёт
  детерминированные случаи (`long-screenshot-receipt`, `structured-product-table`,
  `full-width-banner`, `mixed-language-card`, `generated-simple-paragraph`,
  `generated-product-table`, `generated-low-contrast-noise`,
  `generated-small-skew`).
- Метрики качества живут в `ocr/tests/quality_metrics.py`:
  `token_recall`, `pair_recall`, `digit_sequence_recall`,
  `ordered_phrase_recall`, `markdown_table_shape`, `character_error_rate`.

## PR gate

```bash
npm run format:check
npm run lint
npm test
npm run test:contract
npm run test:smoke
npm run build
docker compose config --quiet
```

PR gate должен завершаться за <5 минут на одной машине. Все tiers, которые не
укладываются (resource, OCR quality, browser quality, Pages, compose), выносятся в
nightly/scheduled jobs.

## Python OCR

```bash
docker build -f docker/ocr.Dockerfile --target test \
  --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
  --build-arg OCR_INSTALL_CJK_FONTS=1 \
  -t ittm-ocr-ci ./ocr

docker run --rm ittm-ocr-ci python -m flake8 .
docker run --rm ittm-ocr-ci python -m black --check .
docker run --rm ittm-ocr-ci python -m ruff check .
docker run --rm ittm-ocr-ci python -m pytest tests -q
```

Если Tesseract/EasyOCR/OpenCV или tessdata недоступны локально, resource и
quality-тесты обязаны `pytest.skip` с явной причиной; молчаливое прохождение без
проверки запрещено.

## Debug

`debug/` содержит ручной A/B corpus: входной файл
`fixtures/name.ext` связан с ручным reference `reference/name.ext.md`.
В git tracked только два SAMPLE-входа и ручные `.md`; реальные локальные
fixtures остаются ignored.
Runtime-артефакты остаются в игнорируемом `debug/tmp/`, а `testtables/`
допускается только как legacy fallback для старых локальных прогонов. Это
**не** PR gate и **не**
доказательство покрытия. Runner-команды, артефакты и heavy pytest описаны в
[debug](./debug.md).
Browser matrix использует Node Canvas preprocessing, но всё равно остаётся
ручной тяжёлой отладкой, а не автоматическим тестом.
`ocr/tests/test_debug_sample_corpus.py` прогоняет tracked SAMPLE fixtures через
backend Tesseract: 4K edge-to-edge слово и hard image-only 10x14 mixed-script
PDF должны оставаться выше debug gate.
`ocr/tests/test_upload_processing.py`, `ocr/tests/test_main.py`,
`gateway/src/core/routes.test.ts` и `gateway/src/cli/run.test.ts` отдельно
проверяют PDF-маршрутизацию: default `auto` берет пригодный text layer без
создания OCR engine, `raster` пропускает text layer и доходит до OCR, а
неизвестные значения отклоняются до обработки файла.
Все PR-safe регрессии обязаны идти через generated fixture registry и метрики
из `quality_metrics.py`.

Запрещено:

- добавлять runtime-файлы `debug/` или `testtables/` в Git вне двух
  `debug/fixtures/SAMPLE*` и `debug/reference/<fixture>.md`;
- описывать ручной набор как замену автоматических тестов;
- использовать ручные таблицы для проверки pipeline профилей без oracle.

## Oracle и pipeline профили

К моменту релиза каждый вызов OCR должен выбирать профиль по oracle, а не по
«кажется похожим». Текущий `ocr/app/pipeline_config.py` уже содержит
`OcrPipelineProfile` и `DEFAULT_ENGINE_PIPELINE_PROFILES`, но oracle выбора
профиля по фиче/типу документа пока живёт только частично.

Что нужно сделать:

1. Сформировать признаки входа: расширение, MIME, размеры, наличие явных
   колонок/сетки, плотность текста, язык, ориентация.
2. Описать oracle contract: вход → рекомендованный `engine_type` + `profile` +
   список флагов (`grid_min_confirmed_cell_ratio`, `direct_region_ocr`,
   `table_word_formatters`, `max_region_height` и т. д.) + ожидаемые
   инварианты результата (`min_token_recall`, `min_pair_recall`,
   `min_digit_recall`, минимальная форма таблицы).
3. Использовать этот oracle в `gateway/src/clients/ocrClient.ts` и в CLI
   `gateway/src/cli/extraction-client.ts` одинаково, чтобы `curl` и Web UI
   получали один и тот же выбор.
4. Каждое новое поле oracle должно быть покрыто тестом: единичные флаги и
   осмысленные наборы флагов.

## Готовые ассеты для quality tier

Когда добавляется новый кейс, нужно:

1. Описать его в `ocr/tests/generated_media.py` как `GeneratedFixtureSpec` или
   `FunctionalOcrFixtureSpec` с `seed`, `category`, `tier`, `expected_tokens` и
   `expected_pairs`.
2. Подключить метрики в `ocr/tests/test_ocr_quality.py` через
   `FUNCTIONAL_QUALITY_MATRIX`.
3. Зафиксировать `min_token_recall`/`min_pair_recall` явно — без них кейс
   считается не покрытым.

## Docker smoke

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS "http://$(docker compose port nginx 80)/api/health"
curl --data-binary @sample.png "http://$(docker compose port nginx 80)/api/extract/text"
```

Сборка образов требует доступа к Docker DNS. Ошибка
`Temporary failure resolving deb.debian.org` относится к build network и должна
перепроверяться в GitHub CI.
