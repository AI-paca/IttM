# Политика безопасности

[English](../en/security.md) | [Документация](./README.md) |
[Ограничения](./architecture-limitations.md)

## Границы доверия

| Режим                     | Куда попадает документ                     |
| ------------------------- | ------------------------------------------ |
| Browser OCR               | Память вкладки и browser workers           |
| Local Tesseract / EasyOCR | Локальные nginx, gateway и Python OCR      |
| Gemini / OpenRouter       | API выбранного внешнего провайдера         |
| Ollama                    | Указанный пользователем локальный endpoint |
| Edge proxy                | Настроенный `ORIGIN_URL` или Gemini        |

Browser и local режимы не эквивалентны: local OCR передаёт файл в Python
backend. Внешний provider означает выход документа за локальную границу
доверия.

## Локальный runtime

- Compose по умолчанию публикует только nginx на `127.0.0.1`.
- Gateway и Python OCR находятся во внутренней Docker-сети.
- Локальный API не имеет аутентификации. Не публикуйте его в недоверенную сеть
  без отдельного auth/reverse-proxy слоя.
- Python CORS выключен, пока не задан allowlist `OCR_CORS_ORIGINS`. Значение
  `*` отклоняется.
- Production OCR container работает от пользователя `ittm` UID/GID 10001.

Web UI отправляет local upload без browser-side Base64. Gateway не сохраняет
документ в database или object storage, но Task API сначала создаёт `File` через
`formData()`/`arrayBuffer()` и оставляет его внутри in-memory task record даже
после terminal state. Наружу сериализуются только имя, размер и media type.
Python читает upload чанками, затем собирает один `bytes` перед OCR. Images
обрабатываются в памяти; PDF временно записывается через `tempfile` для Poppler.

Проект не гарантирует отсутствие данных в swap, crash dumps, proxy logs или
логах хоста. Эти свойства задаются deployment environment.

## Browser OCR

Source file не отправляется backend. Tesseract.js, PDF.js и preprocessing
работают в workers, где это поддерживается. Принятый PDF целиком находится в
worker `ArrayBuffer`; закрытие или перезагрузка вкладки завершает этот
in-memory lifecycle, но не является гарантией безопасного стирания памяти ОС.

## Внешние providers

- UI требует явного согласия перед Gemini/OpenRouter request.
- При прямом вызове пользовательский API key хранится только в frontend state.
- Опциональный Edge worker может хранить `GEMINI_API_KEY` как environment
  secret и проксировать `/api/gemini`.
- Документ может быть отрендерен, уменьшен и закодирован в Base64/data URL.
- После отправки действуют retention и processing policies провайдера.

Не записывайте ключи в tracked `.env`, fixtures, debug reports или исходный
код. Локальные `debug/.env` и `scripts/ollama-deploy/.env` игнорируются.

## Известные риски

- Полный принятый upload существует в памяти Python.
- In-memory task queue не переживает рестарт, не обеспечивает durable retry и
  не удаляет terminal records вместе с исходным `File`.
- Direct gateway Task API не имеет собственного upload cap до buffering;
  `OCR_MAX_UPLOAD_BYTES` срабатывает позже в Python. Compose nginx ограничивает
  внешний запрос раньше.
- Отмена gateway прекращает fetch/reader, но не гарантирует немедленное
  прерывание уже выполняющегося CPU/GPU OCR в Python thread.
- Не все варианты decompression/image bomb могут быть обнаружены до decode.
- После отправки streaming headers ошибка представляется event-ом внутри HTTP 200.
- Edge upload limit полагается на `Content-Length`; origin должен сохранять
  собственный независимый лимит.

Resource limits перечислены в
[`architecture-limitations.md`](./architecture-limitations.md).

## Security checks

```bash
npm run test:sast
npm run test:sca
```

SAST проверяет first-party code. SCA проверяет lockfile, source tree и
container images. Ни один из этих gate не заменяет review deployment topology,
секретов и provider configuration. При ошибке сначала читайте finding из
терминала или generated report; не загружайте целиком `.semgrep/sast.yml`,
workflow или отчёты, если известны rule id, package и image.
