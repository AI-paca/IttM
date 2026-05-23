# Text Extractor (IttM)

Text Extractor конвертирует длинные скриншоты, изображения и PDF-файлы в Markdown. Один кодовый базис запускается в трех формах: локальный Linux/Bun режим, статический Lite-клиент и контейнерный Docker-стек.

Демо: https://ai-paca.github.io/IttM/

## Режимы

| Режим  | Для чего                                                              | Точка входа                    |
| ------ | --------------------------------------------------------------------- | ------------------------------ |
| Local  | Linux workstation, bare metal и быстрый локальный OCR без контейнеров | `bash scripts/run-local.sh`    |
| Lite   | GitHub Pages, слабый VPS со статикой, будущий browser extension UI    | `bash scripts/build-lite.sh`   |
| Docker | VPS/сервер с изолированными nginx, gateway и OCR                      | `docker compose up --build -d` |

В Docker-режиме наружу публикуется только nginx. Gateway и Python OCR остаются внутри Compose-сети.

## Local

Локальный режим рассчитан на host-систему, где удобнее держать gateway и OCR рядом с пользовательским окружением:

```bash
bash scripts/run-local.sh
```

Скрипт:

1. создает `ocr/.venv`, если окружения еще нет;
2. ставит легкие Python-зависимости при первом запуске;
3. запускает FastAPI OCR service;
4. запускает gateway через Bun, а при его отсутствии через Node.js.

Порты по умолчанию:

- web UI и gateway: `http://localhost:3000`;
- Python OCR API: `http://localhost:8000`.

Если порт занят, local-скрипт выберет свободный и напечатает итоговый URL.

Полные локальные Python-зависимости с EasyOCR ставятся отдельно:

```bash
INSTALL_EASYOCR=1 bash scripts/install-local-python.sh
```

Если OCR уже запущен отдельно, gateway можно направить на него через `OCR_URL`:

```bash
OCR_URL=http://127.0.0.1:8000 npm run dev
```

Собранный Node gateway запускается так:

```bash
npm run build
OCR_URL=http://127.0.0.1:8000 npm start
```

## Lite

Lite-сборка содержит браузерный frontend без собственного Docker backend:

```bash
bash scripts/build-lite.sh
```

Результат лежит в `dist/`. Его можно разместить как статику на GitHub Pages или на слабом VPS. GitHub Pages workflow собирает frontend с `VITE_BASE_PATH=/IttM/`.

Browser OCR и настроенные облачные режимы работают из frontend. Упаковка отдельного browser extension UI пока не добавлена, но Lite-сборка остается его статической основой.

## Docker

Контейнерный стек для VPS и серверного запуска поднимается короткой Compose-командой:

```bash
docker compose up --build -d
```

Compose собирает три сервиса:

- `nginx` раздает frontend и проксирует `/api/*`;
- `gateway` принимает API-запросы и ходит в OCR;
- `ocr` запускает Python backend с Tesseract и runtime-зависимостями.

По умолчанию приложение открывается на `http://localhost:3000`.

Остановка контейнеров:

```bash
docker compose down
```

<details>
<summary>Docker variables</summary>

Host-порт и bind nginx можно задать прямо для Compose:

```bash
GATEWAY_HOST_PORT=3001 docker compose up --build -d
GATEWAY_HOST_BIND=0.0.0.0 GATEWAY_HOST_PORT=80 docker compose up --build -d
```

| Переменная              | По умолчанию             | Назначение                       |
| ----------------------- | ------------------------ | -------------------------------- |
| `GATEWAY_HOST_BIND`     | `127.0.0.1`              | Host-интерфейс для nginx.        |
| `GATEWAY_HOST_PORT`     | `3000`                   | Host-порт для nginx.             |
| `NGINX_INTERNAL_PORT`   | `80`                     | Порт nginx внутри Compose.       |
| `GATEWAY_INTERNAL_PORT` | `3000`                   | Порт gateway внутри Compose.     |
| `OCR_INTERNAL_PORT`     | `8000`                   | Порт OCR внутри Compose.         |
| `OCR_REQUIREMENTS`      | `requirements-light.txt` | Requirements-файл для OCR image. |

Docker по умолчанию собирает легкий OCR runtime без предустановленного EasyOCR. `scripts/run-docker.sh` остается Linux helper для автоподбора занятого host-порта и ожидания health endpoint, но не нужен для обычного Compose-запуска:

```bash
bash scripts/run-docker.sh
```

</details>

## Документация

- [Архитектура проекта](./docs/architecture.md)
- [Задания курса](./docs/course_tasks.md)
