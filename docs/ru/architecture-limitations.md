# Ограничения архитектуры

[Документация](./README.md) | [Архитектура](./architecture.md) |
[Безопасность](./security.md)

Значения ниже являются текущими defaults. Переменные окружения могут сужать или
расширять часть backend-лимитов, но внешний proxy всегда может установить более
строгую границу.

| Область              | Текущая граница                                 | Следствие                                             |
| -------------------- | ----------------------------------------------- | ----------------------------------------------------- |
| Compose upload       | nginx `client_max_body_size 32m`                | Запрос больше 32 MiB не попадёт в gateway             |
| Edge upload          | `MAX_UPLOAD_BYTES`, default 32 MiB              | Проверка работает при наличии `Content-Length`        |
| Python upload        | `OCR_MAX_UPLOAD_BYTES`, default 128 MiB         | Upload выше лимита отклоняется                        |
| Gateway task upload  | `formData()` или `arrayBuffer()` без своего cap | Direct gateway может занять память до Python reject   |
| Gateway task memory  | `File` остаётся в in-memory task record         | Terminal task удерживает upload до рестарта gateway   |
| Python upload memory | чанки объединяются в `bytes`                    | Принятый файл целиком существует в RAM                |
| Decoded image        | `OCR_MAX_DECODED_IMAGE_PIXELS`, default 80 MP   | Больший bitmap отклоняется до OCR                     |
| PDF pages            | `OCR_MAX_PDF_PAGES`, default 100                | Более длинный PDF отклоняется                         |
| PDF render           | `OCR_MAX_PDF_RENDER_DIMENSION`, default 6000 px | DPI уменьшается для больших страниц                   |
| PDF temporary files  | Poppler требует путь                            | PDF кратковременно находится в `tempfile`             |
| Dewarp               | `OCR_MAX_DEWARP_PIXELS`, default 16 MP          | Dewarp пропускается при превышении бюджета            |
| Browser PDF          | 128 MiB preflight                               | Принятый PDF целиком читается в worker `ArrayBuffer`  |
| Browser OCR          | 4–14 MP и 2200–4200 px по профилю устройства    | Downscale может потерять мелкие символы               |
| Browser concurrency  | Один worker pool на JS realm вкладки            | Две вкладки независимо конкурируют за CPU/RAM         |
| Web backend stream   | Один Python thread на request, без общего cap   | Две вкладки могут параллельно перегрузить OCR backend |
| Gateway tasks        | 1 worker, до 32 queued                          | Task API сериализует работу внутри процесса           |
| Ollama/provider      | Прямой browser fetch, без очереди IttM          | Параллельность и пределы принадлежат provider         |
| Task persistence     | память процесса                                 | Рестарт удаляет records, events, results и uploads    |
| Streaming error      | headers уже отправлены                          | Поздняя ошибка приходит event-ом внутри HTTP 200      |
| Nginx read timeout   | 300 секунд                                      | Более долгий proxied stream может оборваться          |
| External LLM         | Base64/data URL в браузере                      | Дополнительная память и передача провайдеру           |

## Не предоставляется

- durable queue, retry, retention или восстановление задачи после рестарта;
- object storage для исходных документов или eviction terminal task records;
- аутентификация локального API;
- tile decoder для изображений выше backend decode-limit;
- гарантия отсутствия данных в swap, crash dumps и инфраструктурных логах;
- единый OCR implementation для Python и браузера.

Sparse runtime в `ocr/app/sparse_pipeline` является opt-in библиотечным
контуром и не включён в публичный convert route. Его bounded limits не следует
выдавать за ограничения основного API, пока routing явно не подключён.

`OCR_MAX_UPLOAD_BYTES=128 MiB` ограничивает чтение в Python, а не выделение
памяти gateway Task API. Через Compose раньше срабатывает nginx `32m`; при
прямом доступе к gateway запрос уже может быть полностью прочитан до ответа
Python `413`. Класс `BoundedInputStorage` существует и тестируется отдельно,
но текущий `http-api.ts` его не использует.
