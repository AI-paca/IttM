# IttM (Image-to-Text Markdown)

<p align="right">
  <a href="./docs/ru/README.md"><img alt="Русский" src="https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-%F0%9F%87%B7%F0%9F%87%BA-blue"></a>
  <a href="./docs/en/README.md"><img alt="English" src="https://img.shields.io/badge/English-%F0%9F%87%AC%F0%9F%87%A7-lightgrey"></a>
</p>

[IttM](https://ai-paca.github.io/IttM/) превращает изображения, длинные скриншоты и PDF-документы в Markdown. Приложение поддерживает обработку в браузере, локальные Tesseract/EasyOCR и внешние LLM-провайдеры.

## Возможности

- OCR в браузере без отправки документа на сервер.
- Локальный Tesseract и EasyOCR через Python FastAPI.
- PDF и длинные скриншоты в Markdown.
- Таблицы с ограниченным raw-text fallback.
- Извлечение текстового слоя PDF до запуска OCR.
- Gemini, OpenRouter и Ollama с явным согласием перед внешней отправкой.
- Сборка для [GitHub Pages](https://ai-paca.github.io/IttM/) с локальными Tesseract worker/core assets.

## Режимы обработки

| Режим           | Сетевая зона                                                      | Куда уходит файл                                               | Как это работает внутри                                                                                                 |
| --------------- | ----------------------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Browser         | Внутри вкладки                                                    | Никуда                                                         | Tesseract.js worker в V8; downscale до `2200-4200px` / `4-14MP`; OOM на вкладке при превышении квот.                    |
| Local Tesseract | Backend на loopback / Compose                                     | Multipart-`POST` на `http://127.0.0.1:<port>/api/extract/text` | Gateway проксирует тело в `OCR_URL`; Python собирает upload в `bytes`; decode-guard `80MP`; PDF — `tempfile` Poppler.   |
| Local EasyOCR   | Backend на loopback / Compose                                     | То же                                                          | + VRAM/CPU-fallback: без 6 ГБ VRAM выбран CPU-путь; медленнее, больше RAM.                                              |
| External LLM    | Публичный API: высокий риск на 100-страничном PDF и тонких линиях | По явному согласию — выбранному провайдеру                     | Ключ хранится в state frontend, в Gateway не передаётся; payload уходит по `Content-Type: image/*` или `text/markdown`. |

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
bash scripts/runtime/run-local.sh
```

Статическая сборка без Python backend:

```bash
bash scripts/runtime/build-lite.sh
```

<details>
<summary>Hyprland: скриншот в буфер обмена через pipe</summary>

```bash
grim -g "$(slurp)" - | curl --data-binary @- http://127.0.0.1:3000/api/extract/text | wl-copy
```

CLI-обёртка с тем же контрактом: `npm run extract -- --help`.

</details>

<details>
<summary>PDF: принудительный raster</summary>

```bash
curl --data-binary @plan.pdf \
  "http://127.0.0.1:3000/api/extract/text?filename=plan.pdf&pdf_mode=raster"

npm run extract -- plan.pdf --pdf-mode=raster
```

`pdf_mode=auto` (по умолчанию) — backend проверяет текстовый слой и не запускает OCR, если слой пригоден. `pdf_mode=raster` — пропускает проверку и сразу рендерит/распознаёт каждую страницу. Фактически использованный режим возвращается в `meta.pdf_mode`.

</details>

## Архитектурные границы

| Компонент         | Текущее поведение                                                                   | Ограничение                                                                                    |
| ----------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Browser (OCR/PDF) | Worker, resize до `2200-4200px` / `4-14MP`; постраничный PDF.js + `OffscreenCanvas` | Весь PDF сначала попадает в worker `ArrayBuffer`; downscale может терять мелкие цифры и линии. |
| Gateway           | Потоковое multipart-проксирование и NDJSON                                          | Нет task queue, durable retry и backend cancellation.                                          |
| Python OCR        | Страницы PDF рендерятся по одной; decode guard `80MP`                               | Upload целиком существует в RAM; PDF временно спуливается для Poppler.                         |
| EasyOCR           | CPU fallback без обязательных 6 ГБ VRAM                                             | CPU-путь заметно медленнее и требует больше памяти.                                            |

Полная матрица: [ограничения OCR-архитектуры](./docs/ru/architecture-limitations.md).

## Приватность и жизненный цикл файлов

| Режим / зона                          | Где лежит файл                                                                                   | Как стереть                                                | Границы и где может упасть                                                                                                                                         |
| ------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Browser OCR (вкладка)**             | В памяти вкладки.                                                                                | Закрыть вкладку или перезагрузить ПК.                      | V8 Heap + WebAssembly; Canvas / `OffscreenCanvas` создаётся по странице и освобождается. OOM на больших PNG.                                                       |
| **Локальный gateway**                 | В памяти процесса.                                                                               | Перезапустить `run-local.sh` или контейнер.                | In-memory task queue `maxWorkers: 1`, `maxQueued: 32`. Нет durable retry, нет object storage.                                                                      |
| **Backend OCR (Tesseract / EasyOCR)** | В RAM backend-а, пока идёт распознавание. PDF-страницы кратко попадают в `tempfile` для Poppler. | Дождаться окончания обработки.                             | Upload до 128 MiB (`OCR_MAX_UPLOAD_BYTES`). Decode guard `80MP`. PDF: `max 100` страниц, `max 6000px` на страницу. OOM-killer контейнера снимается cgroup-лимитом. |
| **External LLM**                      | В API провайдера.                                                                                | Отозвать доступ у провайдера; в браузере стереть API-ключ. | Кодирование Base64 на клиенте жрёт ОЗУ и блокирует вкладку до начала сетевого запроса. API-ключ хранится в state frontend, в Gateway не передаётся.                |

Подробности и границы доверия: [политика безопасности](./docs/ru/security.md).

## Проверки

```bash
npm run format:check
npm run lint
npm test
npm run build
npm run build:pages && npm run test:pages
docker compose config --quiet
```

Python-проверки выполняются в OCR CI-образе. Полный список: [тестирование](./docs/ru/testing.md).

## Документация

- [Русская документация](./docs/ru/README.md)
- [English documentation](./docs/en/README.md)
- [Архитектура](./docs/ru/architecture.md)
- [Целевой единый пайплайн](./docs/ru/architecture-unified-pipeline.md)
- [Текущая реализация флагов и профилей](./docs/ru/architecture-current-flags.md)
- [Видение развития проекта](./docs/ru/roadmap/vision.md)
- [Границы ответственности](./docs/ru/course/boundaries.md)
- [Ограничения движка](./docs/ru/architecture-limitations.md)
- [План курса](./docs/ru/course/COURSE_PLAN.md)

## Лицензия

Проект распространяется под лицензией [MIT](./LICENSE).
