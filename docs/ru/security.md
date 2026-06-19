# Политика безопасности

[English](../en/security.md) | [Документация](./README.md)

## Что нельзя считать закрытым

Этот документ описывает текущее состояние, а не маркетинговое заявление. Любой
пункт, которого здесь нет, считается не закрытым.

## Границы доверия

| Вектор                  | Куда попадает документ                               | Уровень риска                                 |
| ----------------------- | ---------------------------------------------------- | --------------------------------------------- |
| Browser OCR             | Tesseract.js worker/PDF.js worker в браузере         | Очень низкий: данные не покидают устройство   |
| Local Tesseract/EasyOCR | nginx → gateway → Python OCR в Docker/локальной сети | Низкий: трафик замкнут loopback/Compose-сетью |
| External LLM            | Gemini, OpenRouter, Ollama по выбору пользователя    | Высокий: SLA и retention определяет вендор    |

## Локальная обработка

- Docker наружу публикует только nginx. Gateway и OCR находятся в
  внутренней сети Compose.
- Локальные режимы передают исходный `File` в multipart без `arrayBuffer()` и
  Base64 на стороне frontend.
- Gateway проксирует request body потоком и не сохраняет документы.
- Python читает upload чанками, но перед OCR объединяет их в полный `bytes`;
  encoded upload по умолчанию ограничен 128 MiB, до decode действует предел
  `OCR_MAX_DECODED_IMAGE_PIXELS` (`80MP`).
- Обычные изображения обрабатываются из памяти. PDF спулируется для Poppler;
  каталог удаляется после запроса.

## Browser OCR

- При выборе Browser документ не отправляется в backend.
- Tesseract.js, PDF.js и resize выполняются в browser workers там, где это
  поддерживается.
- Стандартный профиль оставляет только worker-resize; dewarp вынесен в отдельный
  профиль и по умолчанию не включается.

## External LLM

- Внешняя отправка требует явного согласия пользователя в текущей сессии.
- API-ключ хранится в state frontend и не передаётся в локальный OCR backend.
- Политика retention и хранения после отправки определяется выбранным провайдером.

## Не закрытые риски

- Нет аутентификации на локальном API: защита опирается только на loopback bind.
- In-memory task queue (`maxWorkers: 1`, `maxQueued: 32`) даёт task ID и
  серверную отмену (`POST /api/tasks/:id/cancel`), но не переживает рестарт:
  durable queue, retry и retention отсутствуют.
- Принятый upload до 128 MiB целиком существует в Python `bytes` и в
  `tempfile` для PDF.
- Decoded guard `80MP` не заменяет tile decoder для decompression bomb.
- Ошибка после начала NDJSON отдаётся как `error` event в HTTP 200, а не
  отдельным HTTP status.
