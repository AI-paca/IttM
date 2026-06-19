# Ограничения архитектуры

[Документация](./README.md) | [Движок](./engine/README.md) | [Debug](./debug.md)

Здесь перечислены фактические границы кода в формате причина -> следствие.

## Лимиты

| Область              | Где задано                                     | Причина                                           | Следствие                                              |
| -------------------- | ---------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------ |
| Browser OCR profile  | `web/src/ocr/browser-profile.ts`               | профиль ограничивает pixels/dimension             | resize может ухудшить мелкие цифры                     |
| Browser layout       | `web/src/ocr/image-resize.worker.ts`           | анализ выполняется на уменьшенной копии           | тонкие separators могут исчезнуть                      |
| Browser PDF size     | `web/src/lib/pdf-limits.ts`                    | preflight limit `128 MiB`                         | oversized PDF отклоняется до `arrayBuffer()`           |
| Browser PDF memory   | `web/src/lib/pdf-page.worker.ts`               | принятый PDF читается в `ArrayBuffer`             | файл до 128 MiB целиком находится в памяти worker      |
| External LLM         | `document-encoding.worker.ts`, `llm-client.ts` | API принимает Base64/data URL                     | payload занимает память и уходит выбранному провайдеру |
| Gateway tasks        | `gateway/src/tasks/http-api.ts`                | in-memory queue, `maxWorkers: 1`, `maxQueued: 32` | задачи теряются после рестарта; durable retry нет      |
| Python upload size   | `ocr/app/upload_limits.py`                     | default `OCR_MAX_UPLOAD_BYTES=134217728`          | encoded upload больше 128 MiB отклоняется              |
| Python upload memory | `ocr/app/upload_limits.py`                     | чанки объединяются в `bytes`                      | принятый файл до лимита целиком существует в RAM       |
| Decoded image        | `ocr/app/services/convert_service.py`          | limit `80_000_000` pixels                         | больший bitmap отклоняется до OCR                      |
| PDF pages            | `ocr/app/services/convert_service.py`          | limit `100` pages                                 | более длинный PDF отклоняется                          |
| PDF render           | `ocr/app/services/convert_service.py`          | max dimension `6000`                              | oversized page рендерится с меньшим DPI                |
| Dewarp               | `ocr/app/preprocessing.py`                     | budget `16_000_000` pixels                        | dewarp пропускается на огромном изображении            |
| Table OCR            | `ocr/app/pipeline_config.py`                   | bounded coverage/call flags                       | дорогая или слабая сетка уходит в raw fallback         |
| Long screenshots     | `ocr/app/services/convert_service.py`          | layout получает bounded segments                  | границы сегментов могут влиять на качество             |
| NDJSON errors        | `ocr/app/routers/convert.py`                   | HTTP headers уже отправлены                       | поздняя ошибка приходит event-ом внутри HTTP 200       |

## Что уже закрыто в текущем цикле

- raw `curl` безопасно определяет PDF/PNG/JPEG/WebP по сигнатуре;
- PDF default `auto` использует пригодный text layer, а явный
  `pdf_mode=raster` принудительно проверяет постраничный image path;
- неизвестные engine/profile values отклоняются;
- client disconnect отменяет gateway reader и останавливает producer;
- heartbeat удерживает длинный OCR stream;
- page production имеет backpressure;
- пустые страницы отмечаются `EMPTY_PAGE` и делают результат partial;
- PDF.js decoder assets проверяются Pages gate;
- encoded backend upload и browser PDF имеют default 128 MiB guard.

## Что не решается несколькими строками

- durable queue, object storage, retention и восстановление после рестарта;
- spool входного backend upload без полной копии в Python `bytes`;
- tile-capable decoder для изображений больше 80 MP;
- автоматический oracle между downscale, tiles и quality profile;
- длительный GPU/CPU repeated-request soak;
- новый HTTP status после начала NDJSON response.

## Debug

`debug/` нужен для воспроизведения, а не для заявления о покрытии.
Результат становится regression coverage только после переноса в generated
fixture с ожидаемыми tokens/pairs и порогами.
