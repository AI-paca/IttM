# Ограничения OCR-архитектуры

Состояние локальной PR-ready ветки на 13 июня 2026 года. Browser OCR и backend
OCR остаются независимыми реализациями: браузер использует Tesseract.js/WASM,
backend использует Python, Tesseract и EasyOCR. Общими являются API-контракт и
названия профилей, но не исполняемый preprocessing-код.

## Текущая матрица

| Компонент     | Что уже сделано                                                                                                  | Оставшийся риск                                                                                        |
| ------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Browser OCR   | Standard profile использует worker-resize; лимит зависит от diagnostics: `2200-4200px`, `4-14MP`                 | Downscale может ухудшить мелкие цифры и линии в огромных таблицах                                      |
| Browser PDF   | PDF.js, native text, crop и JPEG работают постранично в module worker с `OffscreenCanvas`                        | Весь файл сначала читается через `file.arrayBuffer()`; старые браузеры используют Main Thread fallback |
| Web upload    | Локальные режимы передают исходный `File` в `FormData` без `arrayBuffer()` и Base64                              | Browser networking и Python всё ещё могут буферизовать данные на своих границах                        |
| Gateway       | Node использует backpressure-aware stream; исходный request body и NDJSON проксируются без прикладной пересборки | Нет task ID, очереди, durable retry и отмены уже запущенного OCR                                       |
| Python upload | Upload читается чанками; decoded image ограничен `80MP`, обычное изображение не создаёт temp-файл                | Чанки объединяются в полный Python `bytes` до начала OCR                                               |
| Backend PDF   | PDF спуливается для Poppler; обычная страница остаётся 300 DPI, oversized downscale до `6000px`                  | Полный upload в RAM существует одновременно с disk-backed PDF                                          |
| Tesseract     | PIL/numpy передаются в `pytesseract` напрямую                                                                    | `pytesseract` запускает subprocess; внутренние ресурсы ОС не являются полностью бездисковыми           |
| EasyOCR       | CUDA/MPS выбирается по доступности, иначе используется CPU; жесткого порога 6 ГБ VRAM нет                        | CPU-путь медленный и держит PyTorch-модели в памяти; GPU на этом хосте не измерен                      |
| External LLM  | Resize/crop/Base64 вынесены с UI-потока; требуется явное согласие                                                | Полный Base64 payload занимает память и отправляется стороннему провайдеру                             |

## Постраничный результат

`/convert/stream` и `/v1/convert/stream` возвращают
`application/x-ndjson`. После распознавания каждой PDF-страницы backend
публикует `page` event, gateway передает его без response buffering, а web
добавляет Markdown сразу. Старый JSON endpoint сохранен; web использует его как
fallback при `404` или `405`.

Это потоковая выдача результата, но не потоковый разбор входа: FastAPI сначала
завершает `read_upload_limited()` и получает полный `bytes`.

## Границы качества

Backend-профили используют `grid_min_confirmed_cell_ratio=0.35`: найденная
морфологией сетка принимается только при достаточной доле подтверждённых
прямоугольных ячеек. Generic word reconstruction требует заполнения `0.35`,
wide curriculum — `0.02` до semantic formatter. Поклеточный fallback ограничен
16 вызовами и принимается только при заполнении `0.5` ячеек. Иначе регион
распознаётся одним raw проходом. Такой путь предпочитает исходный OCR text
разреженной Markdown-таблице, но сам по себе не гарантирует сохранность каждой
цифры.

В эту PR-ready ветку не включены экспериментальные `aligned_rows`, эвристики
преобразования строк диаграммы в таблицу и wall-clock оптимизация под жёсткий
десятисекундный бюджет. Их предыдущая универсализация улучшала отдельные
примеры, но ухудшала локальный Tesseract на других изображениях. Browser
pipeline не получал Python-фильтры и остаётся независимой реализацией.

Текущий backend сохраняет проверенную сегментацию очень длинных изображений и
защиту от сотен OCR-вызовов по ложной таблице. Большие curriculum tables сначала
используют word-level reconstruction, затем ограниченный raw fallback.

## Review и фактические измерения

| Утверждение review                                 | Что подтверждено                                                                        | Что не доказано                                                                     |
| -------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| PDF и Base64 блокируют Main Thread                 | Основной путь вынесен в module/document workers                                         | Main Thread fallback старых браузеров не профилировался                             |
| Standard Browser OCR включает тяжёлый dewarp       | Standard profile оставляет только worker-resize; dewarp изолирован в отдельном профиле  | Отдельный dewarp profile всё ещё синхронный и не должен включаться по умолчанию     |
| Локальный upload создаёт browser-копию файла       | Web-тест запрещает `arrayBuffer()` и проверяет исходный `File` в `FormData`             | Внутреннее буферизование browser networking не измерено                             |
| Gateway повторно собирает multipart                | Node использует `Readable.toWeb`; gateway передаёт тот же `Request.body` в OCR client   | Буферизация внутри runtime/HTTP stack не профилировалась                            |
| Pages снова получит root-relative worker URL       | Verifier поднимает HTTP server и получает `/IttM/vendor/tesseract/*` с кодом 200        | Полный Pages OCR smoke в установленном браузере пока не автоматизирован             |
| Browser падает на изображениях больше `4000x4000`  | Введены пределы dimension/pixels и освобождение canvas/bitmap                           | Нет матрицы размеров, aspect ratio и содержимого, определяющей реальную границу OOM |
| Backend temp files создают высокий I/O             | Обычные images не создают прикладной temp-файл; PDF спуливается один раз для Poppler    | Нет длительного I/O soak test и измерения внутреннего поведения subprocess          |
| Compressed image/PDF page вызывает decoded OOM     | До `image.load()` действует `80MP`; dewarp ограничен `16MP`; oversized PDF снижает DPI  | Порог не заменяет tile decoder и требует настройки под память конкретного хоста     |
| EasyOCR требует не менее 6 ГБ VRAM                 | CPU fallback работает без GPU                                                           | GPU/VRAM границы в текущем окружении не измерены                                    |
| Стриминг решает память больших документов          | Результат отдается постранично и не накапливается в web                                 | Входной upload все еще полностью собирается в Python `bytes`                        |
| Частичный NDJSON смешивается со следующим fallback | После первого page event ошибка завершает запуск и сохраняет только частичный результат | Серверный OCR после client disconnect не отменяется автоматически                   |

`testtables/` является ignored ручным corpus из файлов, которые оказались под
рукой. Скрипты позволяют повторять и сравнивать прогоны, но эти результаты не
являются заявлением о полном покрытии входного пространства.

В частности, corpus пока не покрывает:

- 4K/8K banner с одним крупным словом;
- экстремально узкие и длинные изображения;
- очень маленький текст на изображении с большим pixel count;
- маленький compressed file с огромным decoded bitmap;
- поврежденные, encrypted и многослойные PDF;
- файлы на границе upload-size policy;
- повторные запросы, достаточные для поиска утечки памяти.

Архитектурные пределы должны задаваться минимум двумя независимыми осями:
размером encoded file и decoded pixel/page budget. Время и качество дополнительно
зависят от числа текстовых регионов, таблиц, языков и OCR-вызовов, поэтому один
порог по мегабайтам или разрешению не описывает систему полностью.

## Что не решено

- Настоящий highload требует единого проекта task API, очереди, cancellation,
  object storage и retention policy.
- Backend PDF upload нужен bounded spool без одновременной полной копии в RAM.
- Browser PDF нужен size policy до `arrayBuffer()` и отдельная стратегия для
  очень больших локальных файлов.
- Для изображений больше `80MP` нужен tile-capable decoder вместо увеличения
  текущего decoded budget.
- Профиль огромных изображений с мелкими таблицами должен выбирать между
  downscale, tiles и качеством на основе измерений.
- Нужны GPU EasyOCR benchmark и долгий repeated-request soak test.

## Воспроизводимость

- Backend fixtures: `scripts/benchmark-testtables.sh`
- Browser images: `scripts/benchmark-browser-testtables.sh`
- Browser PDF memory: `scripts/benchmark-browser-pdf-memory.mjs`
