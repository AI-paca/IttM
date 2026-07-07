# Движок и профили

[Документация](../README.md) | [Тестирование](../testing.md) | [Debug](../debug.md)

Эта страница является проверяемым контрактом backend OCR-профилей. CI запускает
`npm run test:engine-docs` и отклоняет изменение
`ocr/app/pipeline_config.py`, если новый профиль, флаг или параметр не упомянут
здесь.

Формат Markdown-таблиц с объединёнными ячейками описан отдельно:
[Markdown-контракт таблиц](table-markdown-contract.md).

## Модель профиля

`OcrPipelineProfile` объединяет:

- `image_preprocessing`;
- Tesseract runtime-флаги `tesseract_language_priority`,
  `text_region_psm`, `document_region_psm`, `wide_text_region_psm`,
  `table_word_psm`, `large_table_word_psm`;
- fallback-флаги широких таблиц `table_raw_text_fallback`,
  `table_raw_text_fallback_psm`, `table_raw_text_fallback_min_rows`,
  `table_raw_text_fallback_min_cols`, `table_raw_text_fallback_max_cols`,
  `table_raw_text_fallback_min_ratio`;
- fallback-флаги sparse text для движков, которые должны оставаться в общей
  трассе качества: `sparse_text_fallback_engine`,
  `sparse_text_fallback_min_tokens`, `sparse_text_fallback_min_ratio`,
  `dense_grid_fallback`, `dense_grid_target_width`,
  `ocr_border_pixels`, внутреннее поле `edge_word_fallback_psms`,
  публикующее повторяемый флаг `edge_word_fallback_psm`,
  `edge_word_fallback_min_tokens`;
- layout-поля `feature_extractors`, `selector`, `allowed_stages`,
  `default_parameters`;
- табличные флаги `grid_min_confirmed_cell_ratio`,
  `table_min_word_cell_coverage`, `wide_table_min_word_cell_coverage`,
  `table_min_cell_coverage`, `max_table_cell_ocr_calls`,
  `table_layout_normalization`, `table_word_recognition`,
  `table_word_formatters`;
- post-OCR contract flag `lexical_correction`.

Параметры layout, используемые текущими профилями: `max_region_height`,
`min_region_height`, `min_separator_coverage`, `direct_region_ocr`,
`medium_page_segmentation`.

## Профили

| Профиль                      | Preprocessing                                                                                        | Layout                                                         | Назначение                            |
| ---------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------- |
| `backend_auto_standard`      | `projector_slide_dewarp`, `mobile_screen_upscale`, `small_text_upscale`, `projected_document_dewarp` | `projection_geometry`, `table_first_heuristic_v1`, `table_regions`, `spatial_regions` | default для `auto`                    |
| `backend_tesseract_standard` | `projector_slide_dewarp`, `mobile_screen_upscale`, `small_text_upscale`, `projected_document_dewarp` | `projection_geometry`, `table_first_heuristic_v1`, `table_regions`, `spatial_regions` | default для Tesseract                 |
| `backend_easyocr_standard`   | `projector_slide_dewarp`, `mobile_screen_upscale`, `small_text_upscale`, `projected_document_dewarp` | `projection_geometry`, `table_first_heuristic_v1`, `table_regions`, `spatial_regions` | default для EasyOCR                   |
| `backend_easyocr_table`      | `projected_document_dewarp`                                                                          | `table_regions`                                                | diagnostic bounded table path EasyOCR |
| `backend_easyocr_spatial`    | `projected_document_dewarp`                                                                          | `table_first_heuristic_v1`, `spatial_regions`, `direct_region_ocr=True` | сложные layout-страницы               |
| `backend_curriculum`         | `projected_document_dewarp`                                                                          | `table_first_heuristic_v1`, `table_regions`, `spatial_regions` | учебные планы и широкие таблицы       |
| `backend_plain_text`         | `projected_document_dewarp`                                                                          | без layout stages                                              | plain text fallback                   |
| `backend_raw`                | пусто                                                                                                | пусто                                                          | сырой OCR без preprocessing/layout    |

Default mapping:

| `engine_type` | Профиль                      |
| ------------- | ---------------------------- |
| `auto`        | `backend_auto_standard`      |
| `tesseract`   | `backend_tesseract_standard` |
| `easyocr`     | `backend_easyocr_standard`   |

Неизвестные `engine_type` и profile name отклоняются, а не переходят молча на
`auto`.

## Табличные флаги

| Флаг                                | Default/значение профиля                        | Эффект                                                                         |
| ----------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------ |
| `grid_min_confirmed_cell_ratio`     | `0.35` в standard-профилях                      | минимальная доля подтвержденных ячеек сетки                                    |
| `table_min_word_cell_coverage`      | `0.35`                                          | порог word-level reconstruction                                                |
| `wide_table_min_word_cell_coverage` | `0.02`                                          | мягкий порог широких curriculum tables                                         |
| `table_min_cell_coverage`           | `0.5`                                           | нижняя граница cell fallback                                                   |
| `max_table_cell_ocr_calls`          | `16`                                            | бюджет OCR-вызовов на таблицу                                                  |
| `table_layout_normalization`        | `logical_columns` / `preserve_grid`             | нормализация колонок                                                           |
| `table_word_recognition`            | `bounded_tiles` / `single_pass_with_left_strip` | стратегия чтения таблицы                                                       |
| `table_word_formatters`             | `generic_markdown` / `curriculum`               | цепочка форматтеров                                                            |
| `table_raw_text_fallback`           | `True` в standard backend profiles              | добавляет sparse raw OCR после больших таблиц                                  |
| `table_raw_text_fallback_psm`       | `11`                                            | PSM для raw fallback широких таблиц                                            |
| `table_raw_text_fallback_min_rows`  | `10`                                            | нижняя граница строк для raw fallback                                          |
| `table_raw_text_fallback_min_cols`  | `8`                                             | нижняя граница колонок для raw fallback                                        |
| `table_raw_text_fallback_max_cols`  | `14`, `30` у EasyOCR                            | верхняя граница колонок для raw fallback                                       |
| `table_raw_text_fallback_min_ratio` | `0.75`                                          | table fallback может быть короче noisy primary, если он полезен для компоновки |
| `dense_grid_fallback`               | `True` в standard backend profiles              | включает multi-pass OCR для плотных A3/landscape-сеток                         |
| `dense_grid_target_width`           | `3300`                                          | минимальная ширина dense-grid рабочего растра                                  |
| `spatial_full_page_fallback`        | `True` в standard backend profiles              | разрешает full-page fallback после spatial segmentation                        |
| `dark_ui_text_fallback`             | `True` в standard backend profiles              | включает text recovery для темных UI screenshots                               |
| `contextual_markdown_grammar`       | `True` для auto/easyocr/curriculum              | включает финальный contextual Markdown repair                                  |
| `structural_output`                 | `markdown`                                      | режим вывода structural layer                                                  |

Raw fallback не заменяет Markdown-таблицу. Он добавляет второй текстовый блок
после таблицы, когда 10x14-style формы со слитыми subsection rows теряют часть
mixed-script текста в cell mapper, но сама сетка нужна для стабильной формы
Markdown и пустых placeholder cells.

Дополнительные dense-grid, sparse-cover и projector passes используют primary
engine, если профиль не объявил recovery engine. Standard EasyOCR profile явно
объявляет Tesseract recovery для A3/curriculum страниц; это не скрыто:
`sparse_text_fallback_engine:tesseract` входит в effective flags, а response
metadata содержит `engine_chain: ["easyocr", "tesseract"]`. Диагностические
`backend_easyocr_table` и `backend_easyocr_spatial` остаются чистыми EasyOCR
profiles без междвижкового recovery.

## OCR-флаги

| Флаг                          | Default/значение профиля  | Эффект                                 |
| ----------------------------- | ------------------------- | -------------------------------------- |
| `tesseract_language_priority` | `rus+eng+kaz+kir+chi_sim+ell+equ` | порядок `-l` языков Tesseract          |
| `text_region_psm`             | `6`                       | PSM компактных text regions            |
| `document_region_psm`         | `3`                       | PSM обычных document-like regions      |
| `wide_text_region_psm`        | `11`                      | PSM широких страниц и sparse layouts   |
| `table_word_psm`              | `6`                       | PSM word-level table OCR               |
| `large_table_word_psm`        | `11`                      | PSM tiled OCR для очень больших таблиц |
| `lexical_correction`          | `off` / `t9_small`        | временный reviewer/post-OCR backend    |
| `ocr_language_retry`          | `off` / `t9_small`        | повторный OCR по языковым кандидатам   |
| `recursive_table_cell_ocr`    | `off` / `auto` / `always` | микропроход OCR по ячейкам recursive grid |
| `recursive_table_cell_ocr_max_calls` | `256`                | бюджет микропроходов OCR по ячейкам   |

## Sparse text fallback

| Флаг                              | Default/значение профиля                     | Эффект                                                               |
| --------------------------------- | -------------------------------------------- | -------------------------------------------------------------------- |
| `sparse_text_fallback_engine`     | `tesseract` в standard EasyOCR, иначе `none` | явно объявленный recovery engine для слабых sparse/dense результатов |
| `sparse_text_fallback_min_tokens` | `18`                                         | минимальный объем fallback-текста                                    |
| `sparse_text_fallback_min_ratio`  | `1.25`                                       | fallback должен быть заметно богаче primary                          |
| `ocr_border_pixels`               | `10`                                         | белое OCR-поле для символов, касающихся края                         |
| `edge_word_fallback_psm`          | `8`, `13` backend; `7` browser               | повторное распознавание краевого слова                               |
| `edge_word_fallback_min_tokens`   | `1`                                          | порог fallback для одного слова, касающегося границ изображения      |

## Layout-флаги

| Поле                     | Текущие значения                             |
| ------------------------ | -------------------------------------------- |
| `feature_extractors`     | `projection_geometry` или пусто              |
| `selector`               | `table_first_heuristic_v1`, `uniform_spatial_v1` или `fixed` |
| `allowed_stages`         | `spatial_regions`, `table_regions` или пусто |
| `default_parameters`     | пары параметр/значение                       |
| `max_region_height`      | `1400` или `2800`                            |
| `min_region_height`      | `300`                                        |
| `min_cell_height`        | `24`                                         |
| `min_region_width`       | `80`                                         |
| `min_separator_coverage` | `0.55`                                       |
| `min_separator_gap`      | `8`                                          |
| `chunk_overlap`          | `16`                                         |
| `max_depth`              | `32`                                         |
| `region_page_dewarp`     | `True`                                       |
| `region_deskew`          | `True`                                       |
| `direct_region_ocr`      | `True` только в spatial EasyOCR profile      |
| `medium_page_segmentation` | `True` в `table_first_heuristic_v1` runtime stage | режет средние multi-section страницы по сильным whitespace bands |

## Effective flag keys

Runtime/debug/API используют сериализованные ключи из `ocr/app/pipeline_flags.py`,
а не отдельные подписи в report-слое. Эти ключи должны оставаться
документированными:

| Ключ                                  | Источник                                         |
| ------------------------------------- | ------------------------------------------------ |
| `preprocess`                          | `OcrPipelineProfile.image_preprocessing`         |
| `ocr_language_priority`               | `OcrPipelineProfile.tesseract_language_priority` |
| `ocr_text_region_psm`                 | `OcrPipelineProfile.text_region_psm`             |
| `ocr_document_region_psm`             | `OcrPipelineProfile.document_region_psm`         |
| `ocr_wide_text_region_psm`            | `OcrPipelineProfile.wide_text_region_psm`        |
| `ocr_table_word_psm`                  | `OcrPipelineProfile.table_word_psm`              |
| `ocr_large_table_word_psm`            | `OcrPipelineProfile.large_table_word_psm`        |
| `lexical_correction`                  | `OcrPipelineProfile.lexical_correction`          |
| `ocr_language_retry`                  | `OcrPipelineProfile.ocr_language_retry`          |
| `recursive_table_cell_ocr`            | `OcrPipelineProfile.recursive_table_cell_ocr`    |
| `recursive_table_cell_ocr_max_calls`  | `OcrPipelineProfile.recursive_table_cell_ocr_max_calls` |
| `structural_output`                   | `OcrPipelineProfile.structural_output`           |
| `table_layout_normalization`          | `OcrPipelineProfile.table_layout_normalization`  |
| `table_slot_builder`                  | `OcrPipelineProfile.table_slot_builder`          |
| `table_word_recognition`              | `OcrPipelineProfile.table_word_recognition`      |
| `max_table_cell_ocr_calls`            | `OcrPipelineProfile.max_table_cell_ocr_calls`    |
| `table_min_cell_coverage`             | `OcrPipelineProfile.table_min_cell_coverage`     |
| `table_min_word_cell_coverage`        | `OcrPipelineProfile.table_min_word_cell_coverage` |
| `wide_table_min_word_cell_coverage`   | `OcrPipelineProfile.wide_table_min_word_cell_coverage` |
| `grid_min_confirmed_cell_ratio`       | `OcrPipelineProfile.grid_min_confirmed_cell_ratio` |
| `table_raw_text_fallback`             | `OcrPipelineProfile.table_raw_text_fallback`     |
| `table_raw_text_fallback_psm`         | `OcrPipelineProfile.table_raw_text_fallback_psm` |
| `table_raw_text_fallback_min_rows`    | `OcrPipelineProfile.table_raw_text_fallback_min_rows` |
| `table_raw_text_fallback_min_cols`    | `OcrPipelineProfile.table_raw_text_fallback_min_cols` |
| `table_raw_text_fallback_max_cols`    | `OcrPipelineProfile.table_raw_text_fallback_max_cols` |
| `table_raw_text_fallback_min_ratio`   | `OcrPipelineProfile.table_raw_text_fallback_min_ratio` |
| `sparse_text_fallback_engine`         | `OcrPipelineProfile.sparse_text_fallback_engine` |
| `sparse_text_fallback_min_tokens`     | `OcrPipelineProfile.sparse_text_fallback_min_tokens` |
| `sparse_text_fallback_min_ratio`      | `OcrPipelineProfile.sparse_text_fallback_min_ratio` |
| `dense_grid_fallback`                 | `OcrPipelineProfile.dense_grid_fallback`         |
| `dense_grid_target_width`             | `OcrPipelineProfile.dense_grid_target_width`     |
| `ocr_border_pixels`                   | `OcrPipelineProfile.ocr_border_pixels`           |
| `edge_word_fallback_psm`              | `OcrPipelineProfile.edge_word_fallback_psms`     |
| `edge_word_fallback_min_tokens`       | `OcrPipelineProfile.edge_word_fallback_min_tokens` |
| `spatial_full_page_fallback`          | `OcrPipelineProfile.spatial_full_page_fallback`  |
| `dark_ui_text_fallback`               | `OcrPipelineProfile.dark_ui_text_fallback`       |
| `contextual_markdown_grammar`         | `OcrPipelineProfile.contextual_markdown_grammar` |
| `layout_selector`                     | `LayoutPipelineConfig.selector`                  |
| `layout_stage`                        | `LayoutPipelineConfig.allowed_stages`            |
| `layout_param:max_region_height`      | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:min_region_height`      | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:min_cell_height`        | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:min_region_width`       | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:min_separator_coverage` | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:min_separator_gap`      | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:chunk_overlap`          | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:max_depth`              | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:region_page_dewarp`     | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:region_deskew`          | `LayoutPipelineConfig.default_parameters`        |
| `layout_param:direct_region_ocr`      | `LayoutPipelineConfig.default_parameters`        |
| `layout_runtime_param:*`              | фактическое решение layout selector              |
| `layout_decision`                     | фактическая метка layout selector                |
| `layout_runtime_stage`                | фактическая layout stage                         |
| `table_word_formatter`                | `OcrPipelineProfile.table_word_formatters`       |
| `ocr_runtime`                         | backend/browser runtime                          |
| `ocr_languages`                       | browser Tesseract.js language order              |
| `ocr_max_dimension`                   | browser profile image dimension limit            |
| `ocr_max_image_pixels`                | browser profile image pixel limit                |
| `browser_cache_worker`                | browser profile worker cache mode                |
| `browser_profile_reason`              | browser profile resource reason                  |
| `pdf_render_scale`                    | browser PDF render scale                         |
| `pdf_mode`                            | API/CLI: `auto` или принудительный `raster`      |
| `pipeline_flags`                      | API/CLI override parameter                       |
| `overrides_enabled`                   | API flag override switch                         |
| `preprocess_runtime`                  | browser/debug runner фактический runtime         |

API публикует каталог через `GET /v1/pipeline/flags`. Параметр
`pipeline_flags` остается fail-closed для общих overrides:
`overrides_enabled` равен `false`, и любые низкоуровневые настройки, кроме
`lexical_correction`, `ocr_language_retry` и `table_slot_builder`, завершают
запрос ошибкой 400. Разрешенные варианты `lexical_correction:t9_small` и
`ocr_language_retry:t9_small` остаются совместимым именем временного reviewer
backend-а; целевой слой проверки кандидатов должен заменяться малой моделью, а
не словарным T9, который переписывает имена и фамилии.

## Tesseract Runtime

Профили хранят приоритет `rus+eng+kaz+kir+chi_sim+ell+equ`, но runtime не смешивает
`kaz` с обычным multi-script проходом: default Tesseract запускается как
`rus+eng+kir+chi_sim+ell+equ`, если соответствующие traineddata установлены.
`kaz` включается только при явном первом приоритете, например `kaz+rus+eng`.
Это сохраняет поддержку казахского OCR, не ухудшая обычные русско-английские
изображения похожими кириллическими символами. `ell` и `equ` являются
опциональной базой для греческого текста и старого Tesseract equation OCR:
без установленных `ell.traineddata`/`equ.traineddata` runtime отфильтрует их и
пойдёт по доступным языкам.

Для text-mode OCR движок добавляет внутреннюю белую рамку 10 px. Это не меняет
исходный файл, но стабилизирует случаи, где слово или строка касается границ
изображения.

При `ocr_language_retry:t9_small` Tesseract ведет вероятностную таблицу языков
для текущего движка. Каждый распознанный регион обновляет веса по фактическим
скриптам текста; следующие регионы проверяются через смешанный язык профиля,
все одиночные языки профиля и дополнительные Greek/math кандидаты. Выбор делает
reviewer по качеству текста, а не словарь исправлений.

PDF pages рендерятся постранично. Большие широкие страницы распознаются с
`psm=11` вместо uniform-block `psm=6`, потому что учебные планы, обложки и
wide tables редко являются одним ровным текстовым блоком.

До рендера действует отдельный публичный PDF-контракт. `pdf_mode=auto`
сначала проверяет пригодность встроенного текста и возвращает его без
дорогого OCR; если текстового слоя нет, backend автоматически переходит к
постраничному рендеру. `pdf_mode=raster` явно пропускает текстовый слой и
нужен для сканов, curl-проверок и воспроизведения image path.

## Проверка и отладка

- gateway/CLI tests проверяют передачу `engine_type` и `pipeline_profile`;
- backend tests проверяют resolution, layout/table behavior и fail-closed
  неизвестных значений;
- `scripts/debug/run-debug.sh` пишет effective flags в `profiles.json` и
  `summary.tsv`;
- CI запускает `scripts/ci/verify_engine_profile_docs.py` и
  `scripts/ci/verify_pipeline_flag_docs.py`; второй verifier сверяет каталог
  `pipeline_flags.py` и flag-ключи, найденные в browser/debug scripts.

Ручной вызов:

```bash
curl -F file=@sample.png \
  "http://127.0.0.1:3000/api/extract/text?engine_type=tesseract&pipeline_profile=backend_tesseract_standard"

curl --data-binary @plan.pdf \
  "http://127.0.0.1:3000/api/extract/text?filename=plan.pdf&pdf_mode=raster"
```

При добавлении профиля нужно обновить код, тесты контракта и эту страницу.
