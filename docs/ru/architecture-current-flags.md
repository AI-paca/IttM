# Текущая реализация флагов и профилей

[Архитектура](./architecture.md) | [Целевой единый пайплайн](./architecture-unified-pipeline.md) | [Движок](./engine/README.md)

Этот документ фиксирует, **что именно сейчас находится в коде** в части профилей, флагов и режимов. Целевая модель описана в [`architecture-unified-pipeline.md`](./architecture-unified-pipeline.md); отличия — в таблице «Что ещё не сходится» там же.

## Где живёт правда

| Слой                         | Файл                                             | Что внутри                                                                                                                             |
| ---------------------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Каталог профилей             | `ocr/app/pipeline_config.py`                     | `OcrPipelineProfile`, `LayoutPipelineConfig`, `OCR_PIPELINE_PROFILES`, `DEFAULT_ENGINE_PIPELINE_PROFILES`, `resolve_pipeline_profile`. |
| Сериализация effective flags | `ocr/app/pipeline_flags.py`                      | `profile_flag_items`, `profile_flags`, `pipeline_flag_catalog`, `pipeline_flags_payload`.                                              |
| Маршрутизация engine         | `ocr/app/routers/convert.py`                     | `resolve_pipeline_profile` + `normalize_pdf_mode` + опрос `pipeline_flags`.                                                            |
| Сервис                       | `ocr/app/services/convert_service.py`            | `normalize_pdf_mode`, `_extract_pdf_text_layer_pages`, итерация страниц.                                                               |
| Browser mirror               | `web/src/ocr/*` (browser-profile.ts, flag sweep) | Свой debug-источник effective flags, синхронизирован CI verifier'ом с `pipeline_flags.py`.                                             |
| API-каталог                  | `GET /v1/pipeline/flags`                         | `pipeline_flag_catalog()` + профилированные sets.                                                                                      |

## Как клиент выбирает профиль

Клиент передаёт `engine_type` и опциональный `pipeline_profile` через query/header/JSON/CLI:

```
engine_type: auto | tesseract | easyocr
pipeline_profile: <имя из OCR_PIPELINE_PROFILES> | (default по engine_type)
pdf_mode: auto | raster
pipeline_flags: <reserved, disabled>
```

`resolve_pipeline_profile` в `ocr/app/pipeline_config.py`:

1. Если `pipeline_profile` не задан — взять default для `engine_type` (`backend_auto_standard` / `backend_tesseract_standard` / `backend_easyocr_standard`).
2. Если `pipeline_profile` задан — искать строго в `OCR_PIPELINE_PROFILES`.
3. Любое неизвестное значение → `ValueError`, gateway возвращает HTTP 400.

## Как работает `pipeline_flags`

`pipeline_flags` зарезервирован в публичном контракте, но **не реализован**:

```python
def ensure_flag_overrides_allowed(raw_flags: str | None) -> None:
    if not raw_flags:
        return
    raise ValueError("Pipeline flag overrides are not implemented yet.")
```

Включение требует `OCR_PIPELINE_FLAG_OVERRIDES=1` **и** реализации override resolver. Сейчас:

- `overrides_enabled` отдаётся в `pipeline_flags_payload()` и через `GET /v1/pipeline/flags`.
- Любой непустой `pipeline_flags` отклоняется с HTTP 400.

Это сознательно: будущие LLM-движки (включая маленькие модели типа Gemma) должны подключаться к общему flag resolver, а не получать изолированную строку настроек.

## Каталог effective flag keys

`pipeline_flag_catalog()` объединяет ключи из всех профилей + фиксированные browser/debug ключи:

| Ключ                                                      | Источник                                         |
| --------------------------------------------------------- | ------------------------------------------------ |
| `preprocess`                                              | `OcrPipelineProfile.image_preprocessing`         |
| `ocr_language_priority`                                   | `OcrPipelineProfile.tesseract_language_priority` |
| `ocr_text_region_psm`                                     | `OcrPipelineProfile.text_region_psm`             |
| `ocr_document_region_psm`                                 | `OcrPipelineProfile.document_region_psm`         |
| `ocr_wide_text_region_psm`                                | `OcrPipelineProfile.wide_text_region_psm`        |
| `ocr_table_word_psm`                                      | `OcrPipelineProfile.table_word_psm`              |
| `ocr_large_table_word_psm`                                | `OcrPipelineProfile.large_table_word_psm`        |
| `table_raw_text_fallback*`                                | `OcrPipelineProfile.table_raw_text_fallback*`    |
| `sparse_text_fallback_*`                                  | `OcrPipelineProfile.sparse_text_fallback_*`      |
| `dense_grid_fallback`, `dense_grid_target_width`          | `OcrPipelineProfile.*`                           |
| `ocr_border_pixels`                                       | `OcrPipelineProfile.ocr_border_pixels`           |
| `edge_word_fallback_psm`, `edge_word_fallback_min_tokens` | `OcrPipelineProfile.*`                           |
| `layout_selector`                                         | `LayoutPipelineConfig.selector`                  |
| `layout_stage`                                            | `LayoutPipelineConfig.allowed_stages`            |
| `layout_param:*`                                          | `LayoutPipelineConfig.default_parameters`        |
| `table_word_formatter`                                    | `OcrPipelineProfile.table_word_formatters`       |
| `table_*`                                                 | `OcrPipelineProfile.table_*`                     |
| `ocr_runtime`                                             | backend/browser runtime                          |
| `ocr_languages`                                           | browser Tesseract.js language order              |
| `ocr_max_dimension`                                       | browser profile image dimension limit            |
| `ocr_max_image_pixels`                                    | browser profile image pixel limit                |
| `browser_cache_worker`                                    | browser profile worker cache mode                |
| `browser_profile_reason`                                  | browser profile resource reason                  |
| `pdf_render_scale`                                        | browser PDF render scale                         |
| `pdf_mode`                                                | API/CLI: `auto` или `raster`                     |
| `preprocess_runtime`                                      | browser/debug runner фактический runtime         |
| `pipeline_flags`                                          | API query contract (disabled)                    |
| `overrides_enabled`                                       | API query contract (false)                       |

Полный перечень поддерживается в [`docs/ru/engine/README.md`](./engine/README.md) и проверяется `scripts/ci/verify_engine_profile_docs.py` + `scripts/ci/verify_pipeline_flag_docs.py`.

## Текущее PDF-поведение

`pdf_mode` в API/CLI/JSON/header:

| Значение | Поведение в `convert_service.py`                                                                                                |
| -------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `auto`   | Вызвать `_extract_pdf_text_layer_pages`; если есть пригодные страницы — вернуть текст без OCR. Иначе — постраничный render+OCR. |
| `raster` | Пропустить `_extract_pdf_text_layer_pages`; сразу постраничный render+OCR.                                                      |

`_extract_pdf_text_layer_pages` использует `pdftotext` (из Poppler) постранично и применяет:

- `PDF_TEXT_LAYER_MIN_CHARS=200`
- `PDF_TEXT_LAYER_MIN_WORDS=20`
- `PDF_TEXT_LAYER_MIN_PAGE_RATIO=0.5` (если меньше половины страниц пригодны — слой игнорируется)

Фактически использованный режим возвращается в `meta.pdf_mode`.

## Текущие engines

| Engine      | Класс             | Где                                                                        |
| ----------- | ----------------- | -------------------------------------------------------------------------- |
| `auto`      | `AutoEngine`      | `ocr/app/engines/auto_engine.py` (Tesseract primary + Tesseract fallback). |
| `tesseract` | `TesseractEngine` | `ocr/app/engines/tesseract_engine.py`                                      |
| `easyocr`   | `EasyOcrEngine`   | `ocr/app/engines/easyocr_engine.py`                                        |
| `stub`      | `StubEngine`      | debug-only, отказывает в распознавании.                                    |

`auto`-движок не пытается использовать EasyOCR; он сделан для сценариев, где EasyOCR недоступен и нужен Tesseract primary + Tesseract fallback. Recovery в EasyOCR включается через `sparse_text_fallback_engine` в профиле, а не через `auto`.

## Сейчас vs целевая модель — сводка

| Аспект               | Сейчас                                                           | Целевая модель                                                                |
| -------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Каталог профилей     | backend-local: `OCR_PIPELINE_PROFILES` в `pipeline_config.py`    | один и тот же реестр виден в backend, browser, LLM через `/v1/pipeline/flags` |
| Override resolver    | `pipeline_flags` отклоняется (HTTP 400)                          | резолвер включён, `overrides_enabled=true`                                    |
| Browser flags        | mirror в `web/src/ocr/*` + CI verifier                           | источник — `/v1/pipeline/flags`                                               |
| `meta.engine_chain`  | присутствует в EasyOCR standard; не описан в публичном контракте | описан в OpenAPI и стабильно возвращается всеми движками                      |
| `meta.pdf_mode`      | присутствует                                                     | задокументирован как обязательный                                             |
| Launcher abstraction | Docker/bare-metal/Lite отличаются командами, но не API           | self-documenting launcher (`--help` печатает контракт)                        |

## Где смотреть детали

- Каталог профилей, таблицы табличных/OCR-флагов и поведение Tesseract runtime: [`docs/ru/engine/README.md`](./engine/README.md).
- Жёсткие лимиты, причины и следствия: [`docs/ru/architecture-limitations.md`](./architecture-limitations.md).
- Целевая модель: [`docs/ru/architecture-unified-pipeline.md`](./architecture-unified-pipeline.md).
