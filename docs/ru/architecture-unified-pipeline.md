# Единый pipeline contract и runtime adapters

[Архитектура](./architecture.md) | [Pipeline runbook](./pipeline/README.md) |
[Debug sample](../../debug/EXAMPLE.md) |
[Текущие flags](./architecture-current-flags.md)

Один логический pipeline задаёт порядок:

```text
align → segment → project_sparse → recognize_segments
      → select_language_candidate → lexical_correction
      → group_structures → render_markdown
```

Capabilities входного artifact выбирают нужное подмножество этапов. Например,
trusted Markdown не проходит layout, retry, correction и повторный render.

![Внутренние этапы единого pipeline](../assets/ocr-pipeline.svg)

Редактируемый источник:
[`ocr-pipeline.drawio`](../assets/ocr-pipeline.drawio). Это раскрытие блока
`PIPELINE ENGINE` из [общей архитектурной схемы](../assets/project-architecture.svg);
entry routes, очереди и output adapters здесь намеренно не повторяются.

Entry routes заканчиваются на artifact contract. Они определяют способ
доставки и доступный runtime, но не являются владельцами OCR engine.
Четыре внешние рамки на схеме — фазы, блоки внутри них — stage contracts.
Runtime adapters вложены только в свою recognition stage. Tesseract.js либо
Python Tesseract/EasyOCR вызываются из `recognize_segments`: сплошная стрелка
показывает call, штриховая — return. После возврата стадия выпускает image-free
`RecognizedSegment[]`; он продолжает pipeline до structural grouping и Markdown
render.

Текущая реализация не притворяется одним центральным controller. Browser
alignment/segmentation происходят до `runTextPipeline`, language retry в обоих
runtime пока связан с recognition adapter, а dedicated browser handler
`project_sparse` не зарегистрирован.

| Stage contracts        | Browser runtime                  | Python runtime                      |
| ---------------------- | -------------------------------- | ----------------------------------- |
| align, segment         | До `runTextPipeline`             | Page decode и layout analysis       |
| project_sparse         | Dedicated handler отсутствует    | Sparse codes; полная matrix — debug |
| recognize, language    | Tesseract.js worker и reviewer   | OCR adapter и language agenda       |
| lexical, group, render | Зарегистрированные text handlers | Formatting и structural journal     |

## Что действительно общее

`pipeline-core/src/lib.rs` и `candidates.rs` — один Rust source. Из него
собираются:

- `libittm_pipeline_core.so`, который загружает Python;
- `ittm_pipeline_core.wasm`, который загружает browser runtime.

Общими являются recipe mask, sparse codes, evidence score, primary replacement
и text-block deduplication. Сборка проверяет Rust tests, Python/Rust parity и
WASM ABI.

## Что остаётся платформенным

| Runtime  | Исполнитель                                                   |
| -------- | ------------------------------------------------------------- |
| Browser  | PDF.js/image tiles, Tesseract.js, TypeScript handlers         |
| Backend  | PDF/image decode, Tesseract/EasyOCR, Python layout и handlers |
| Provider | trusted Markdown artifact после consent/configuration         |

Это один контракт и одна Rust decision-логика, но не один общий OCR engine.

## Текущие и будущие входы

Сейчас работают Web browser source, Web backend stream, CLI/Task API и external
provider path. Web UI вызывает Ollama прямым browser `fetch`, поэтому этот путь
не проходит через gateway queue.

Главные ограничения текущего кода:

- browser OCR: отдельный worker pool на вкладку, общей очереди между вкладками
  нет;
- compatibility backend stream: отдельный Python thread на запрос, общего OCR
  concurrency cap нет;
- Task API: один worker и очередь до 32 ожидающих задач;
- Ollama/provider: прямые запросы, расписание принадлежит provider;
- compute: OCR recognition; в полном debug sample block OCR занял около 78 с.

Browser extension на схеме — штрихованный вход с пока не выбранным transport.
Ручной Hyprland pipe уже входит через `curl /api/extract/text`; штриховкой
показан только отсутствующий пакетированный capture UI/lifecycle. Этот путь
использует HTTP Task API, а не WASM.

Полные artifacts и время этапов:
[debug/EXAMPLE.md](../../debug/EXAMPLE.md).
