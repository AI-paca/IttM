# Единый pipeline contract и runtime adapters

[Архитектура](./architecture.md) | [Pipeline runbook](./pipeline/README.md) |
[Debug sample](../../debug/EXAMPLE.md) |
[Текущие flags](./architecture-current-flags.md)

Один исполняемый raster pipeline задаёт порядок из
`debug-all-separated.sh`:

```text
preprocess → geometry → topology → find-object → separate-block
           → ocr-blocks → get-segment → generate-object
```

Raster-документ проходит все восемь этапов. Надёжный native PDF text layer —
единственный shortcut; `PDF как изображение` принудительно возвращает его в
raster pipeline.

![Внутренние этапы единого pipeline](../assets/ocr-pipeline.svg)

Редактируемый источник:
[`ocr-pipeline.drawio`](../assets/ocr-pipeline.drawio). Это раскрытие блока
`PIPELINE ENGINE` из [общей архитектурной схемы](../assets/project-architecture.svg);
entry routes, очереди и output adapters здесь намеренно не повторяются.

Entry routes заканчиваются на artifact contract. Они определяют способ
доставки и доступный runtime, но не являются владельцами OCR engine.
Runtime adapters вложены только в `ocr-blocks`. Tesseract.js, нативный
Tesseract C API либо Python EasyOCR получают crop jobs от Rust и возвращают
текст и word evidence; Rust сохраняет
source order и выполняет `get-segment`/`generate-object`.

| Stage contracts              | Browser runtime       | Native runtime                   |
| ---------------------------- | --------------------- | -------------------------------- |
| preprocess … separate-block  | `pipeline-core` WASM  | native `pipeline-core`           |
| ocr-blocks                   | Tesseract.js/provider | Tesseract C API / EasyOCR worker |
| get-segment, generate-object | `pipeline-core` WASM  | native `pipeline-core`           |

## Что действительно общее

`pipeline-core/src/lib.rs` и `candidates.rs` — один Rust source. Из него
собираются:

- `libittm_pipeline_core.so`, который загружает Python;
- `ittm_pipeline_core.wasm`, который загружает browser runtime.

`ocr-runtime` также подключает ядро как Rust crate через безопасный `Session`.
Он владеет HTTP/NDJSON, загрузкой изображений/PDF, циклом OCR jobs и debug CLI.
Это backend по умолчанию в Compose и `run-local.sh`; прежний Python backend
сохранён для совместимости и сравнительных тестов.

Общими являются stage controller, raster block geometry, source order и
сборка результата, а также bounded recipe/evidence helpers. Сборка проверяет
Rust tests, Python/Rust parity и WASM ABI 6.

## Что остаётся платформенным

| Runtime  | Исполнитель                                         |
| -------- | --------------------------------------------------- |
| Browser  | PDF.js/image decode и Tesseract.js/provider adapter |
| Backend  | PDF/image decode и Tesseract/EasyOCR adapter        |
| Provider | Сетевой OCR adapter после consent/configuration     |

OCR engine платформенный, но маршрут до/после его вызова один и реализован Rust.

## Текущие и будущие входы

Сейчас работают Web browser source, Web backend stream, CLI/Task API и external
provider path. Web UI вызывает Ollama прямым browser `fetch`, поэтому этот путь
не проходит через gateway queue.

Главные ограничения текущего кода:

- browser OCR: отдельный worker pool на вкладку, общей очереди между вкладками
  нет;
- Rust backend: `OCR_CONCURRENCY=1` по умолчанию, при занятости возвращает 429;
  закрытие NDJSON stream отменяет обработку;
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
