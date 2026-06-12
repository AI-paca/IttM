# Ограничения OCR-архитектуры

Состояние локальной PR-ready ветки на 12 июня 2026 года. Browser OCR и backend
OCR остаются независимыми реализациями: браузер использует Tesseract.js/WASM,
backend использует Python, Tesseract и EasyOCR. Общими являются API-контракт и
названия профилей, но не исполняемый preprocessing-код.

## Текущая матрица

| Компонент | Что уже сделано | Оставшийся риск |
| --- | --- | --- |
| Browser OCR | Tesseract.js работает в отдельном worker; изображения ограничены `4096px` и `12MP` | Downscale может ухудшить мелкие цифры и линии в огромных таблицах |
| Browser PDF | PDF.js, native text, crop и JPEG работают постранично в module worker с `OffscreenCanvas` | Весь файл сначала читается через `file.arrayBuffer()`; старые браузеры используют Main Thread fallback |
| Gateway | Multipart-запрос и NDJSON-ответ проксируются без накопления полного ответа | Нет task ID, очереди, durable retry и отмены уже запущенного OCR |
| Python upload | Upload читается чанками, обычное изображение не создает прикладной temp-файл | Чанки объединяются в полный Python `bytes` до начала OCR |
| Backend PDF | PDF временно спуливается для Poppler, страницы рендерятся по одной, каталог удаляется | Полный upload в RAM существует одновременно с disk-backed PDF |
| Tesseract | PIL/numpy передаются в `pytesseract` напрямую | `pytesseract` запускает subprocess; внутренние ресурсы ОС не являются полностью бездисковыми |
| EasyOCR | CUDA/MPS выбирается по доступности, иначе используется CPU; жесткого порога 6 ГБ VRAM нет | CPU-путь медленный и держит PyTorch-модели в памяти; GPU на этом хосте не измерен |
| External LLM | Resize/crop/Base64 вынесены с UI-потока; требуется явное согласие | Полный Base64 payload занимает память и отправляется стороннему провайдеру |

## Постраничный результат

`/convert/stream` и `/v1/convert/stream` возвращают
`application/x-ndjson`. После распознавания каждой PDF-страницы backend
публикует `page` event, gateway передает его без response buffering, а web
добавляет Markdown сразу. Старый JSON endpoint сохранен; web использует его как
fallback при `404` или `405`.

Это потоковая выдача результата, но не потоковый разбор входа: FastAPI сначала
завершает `read_upload_limited()` и получает полный `bytes`.

## Границы качества

В эту PR-ready ветку не включены экспериментальные `aligned_rows`, эвристики
преобразования диаграмм в таблицы и новые Python/browser table filters. Их
предыдущая универсализация улучшала отдельные примеры, но ухудшала локальный
Tesseract на других изображениях. Эксперименты продолжаются только в отдельной
дочерней ветке с A/B результатами по каждому движку.

Текущий backend сохраняет проверенную сегментацию очень длинных изображений и
защиту от тысяч OCR-вызовов по ложной таблице. Большие curriculum tables
сохраняют прежний fallback.

## Что не решено

- Настоящий highload требует единого проекта task API, очереди, cancellation,
  object storage и retention policy.
- Backend PDF upload нужен bounded spool без одновременной полной копии в RAM.
- Browser PDF нужен size policy до `arrayBuffer()` и отдельная стратегия для
  очень больших локальных файлов.
- Профиль огромных изображений с мелкими таблицами должен выбирать между
  downscale, tiles и качеством на основе измерений.
- Нужны GPU EasyOCR benchmark и долгий repeated-request soak test.

## Воспроизводимость

- Backend fixtures: `scripts/benchmark-testtables.sh`
- Browser images: `scripts/benchmark-browser-testtables.sh`
- Browser PDF memory: `scripts/benchmark-browser-pdf-memory.mjs`
