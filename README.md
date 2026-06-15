# IttM (Image-to-Text Markdown)

[Русский](./docs/ru/README.md) | [English](./docs/en/README.md)

[IttM](https://ai-paca.github.io/IttM/) преобразует изображения, длинные
скриншоты и PDF-документы в Markdown. Приложение поддерживает обработку в
браузере, локальные Tesseract/EasyOCR и внешние LLM-провайдеры.

## Возможности

- Browser OCR на Tesseract.js/WASM без отправки документа на сервер.
- Локальный Tesseract и EasyOCR через Python FastAPI.
- Постраничная выдача PDF через NDJSON.
- Распознавание таблиц с ограниченным raw-text fallback.
- Native text extraction из PDF до запуска OCR.
- Gemini, OpenRouter и Ollama с явным согласием перед внешней отправкой.
- GitHub Pages-сборка с локальными Tesseract worker/core assets.

## Режимы обработки

| Режим           | Где выполняется                | Передача исходного файла                                     |
| --------------- | ------------------------------ | ------------------------------------------------------------ |
| Browser         | Tesseract.js worker в браузере | Файл не покидает вкладку                                     |
| Local Tesseract | Python backend                 | Исходный `File` отправляется multipart-потоком через gateway |
| Local EasyOCR   | Python backend                 | Исходный `File` отправляется multipart-потоком через gateway |
| External LLM    | API выбранного провайдера      | Только после явного согласия пользователя                    |

Для локальных режимов frontend не декодирует файл через `arrayBuffer()` и не
кодирует его в Base64. Gateway передает исходное тело запроса в OCR backend.
Python пока собирает upload в `bytes` перед OCR; это зафиксированное ограничение
backend, а не браузера.

## Быстрый старт

### Docker Compose

```bash
docker compose up -d
docker compose port nginx 80
```

Откройте адрес, который вернула вторая команда. Проверка API:

```bash
curl -fsS "http://127.0.0.1:<порт>/api/health"
```

Подробные команды: [ручной запуск Docker](./docs/ru/docker-manual-launch.md).

### Локальная разработка

Требуются Bun, Python 3.10+, Tesseract и Poppler:

```bash
bash scripts/run-local.sh
```

Статическая сборка без Python backend:

```bash
bash scripts/build-lite.sh
```

### Быстрое извлечение из Hyprland

Если локальный gateway слушает `127.0.0.1:3000`, выделенную область экрана можно
распознать и сразу положить в буфер обмена:

```bash
grim -g "$(slurp)" - | curl --data-binary @- http://127.0.0.1:3000/api/extract/text | wl-copy
```

## Архитектурные границы

| Компонент   | Текущее поведение                                      | Ограничение                                                           |
| ----------- | ------------------------------------------------------ | --------------------------------------------------------------------- |
| Browser OCR | Worker, resize до `2200-4200px`/`4-14MP`               | Downscale может терять мелкие цифры и линии                           |
| Browser PDF | Постраничный PDF.js worker и `OffscreenCanvas`         | Весь PDF сначала попадает в worker `ArrayBuffer`                      |
| Gateway     | Потоковое multipart-проксирование и NDJSON             | Нет task queue, durable retry и backend cancellation                  |
| Python OCR  | Страницы PDF рендерятся по одной; decoded guard `80MP` | Upload целиком существует в RAM; PDF временно спуливается для Poppler |
| EasyOCR     | CPU fallback без обязательных 6 ГБ VRAM                | CPU-путь заметно медленнее и требует больше памяти                    |

Полная матрица:
[ограничения OCR-архитектуры](./docs/ru/architecture-limitations.md).

## Приватность

- Browser OCR не отправляет документ по сети.
- Docker публикует наружу только nginx; gateway и OCR остаются во внутренней
  сети.
- Локальный gateway не сохраняет документы в базе данных.
- Внешний LLM получает документ только после явного согласия.
- API-ключи внешних провайдеров не отправляются в локальный OCR backend.

Подробнее: [политика безопасности](./docs/ru/security.md).

## Проверки

```bash
npm run format:check
npm run lint
npm test
npm run build
npm run build:pages && npm run test:pages
docker compose config --quiet
```

Python-проверки выполняются в OCR CI-образе. Полный список:
[тестирование](./docs/ru/testing.md).

## Документация

- [Русская документация](./docs/ru/README.md)
- [English documentation](./docs/en/README.md)
- [Архитектура](./docs/ru/architecture.md)
- [Границы ответственности](./docs/ru/course/boundaries.md)
- [Ограничения движка](./docs/ru/architecture-limitations.md)
- [История усиления движка](./docs/ru/engine-hardening-progress.md)
- [План курса](./docs/ru/course/COURSE_PLAN.md)
- [SBOM / зависимости](./docs/ru/sbom-report.md)
