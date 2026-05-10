Веб-приложение для конвертации длинных скриншотов экрана в Markdown

## [Запуск](https://ai-paca.github.io/IttM/)

```bash
bash run.sh
```

_(Сервер запустится на :3000)_

Если `3000` или `8000` заняты, локальные скрипты выбирают ближайшие свободные host-порты и печатают итоговые URL.

Для GitHub Pages сборка использует `VITE_BASE_PATH=/IttM/`; локальный `run.sh` собирает bundle с `VITE_BASE_PATH=/`, чтобы assets грузились с localhost без префикса репозитория.

`run.sh` рассчитан на слабую VPS: по умолчанию ставит только легкие runtime-зависимости из `ocr/requirements-light.txt`, не запускает тесты, не трогает Docker и не ставит PyTorch/EasyOCR. Тяжелый OCR включается отдельно через кнопку установки в UI или явно:

```bash
INSTALL_EASYOCR=1 bash run.sh
```

Если `dist/` уже собран, `run.sh` переиспользует его для быстрого рестарта. Для пересборки frontend:

```bash
FORCE_BUILD=1 bash run.sh
```

### Режимы запуска

- **GitHub Pages**: статический frontend, распознавание через browser OCR и LLM/API-режимы при доступности.
- **Bun local**: легкий gateway adapter без тяжелого Node-сервера; обычный `run.sh` не гоняет тесты и не ставит EasyOCR/PyTorch.
- **Node gateway**: основной production-friendly режим для Cloud Run, AI Studio, canvas/hosted-сред и локального `node`.
- **Local Python OCR**: FastAPI backend с Tesseract/EasyOCR за gateway.
- **Hybrid local+node/bun**: frontend/gateway локально, OCR backend отдельно через `OCR_URL`.

## CI и базовые проверки

Локально:

```bash
npm run debug
```

Быстро без Docker/act:

```bash
npm run debug -- --no-docker --no-act
```

Полная очистка Docker-кэшей включается только явно:

```bash
npm run debug -- --clean
```

Если Docker спотыкается из-за корпоративного firewall/daemon state, `scripts/debug.sh` вызывает `scripts/notify-docker-restart.sh`: он подает звук, показывает уведомление и ждёт ручного рестарта Docker.

Docker Compose не требует свободных `3000`/`8000`: `npm run debug` выставляет `GATEWAY_HOST_PORT` и `OCR_HOST_PORT` динамически. Для ручного запуска можно задать их явно:

```bash
GATEWAY_HOST_PORT=3001 OCR_HOST_PORT=8001 docker compose up --build
```

Основные команды, которые повторяет GitHub Actions:

```bash
npm ci
npm run format:check
npm run lint
npm test
npm run build
python -m pip install -r ocr/requirements-ci.txt
python -m pytest ocr/tests -q
RUN_OCR_QUALITY=1 python -m pytest ocr/tests/test_ocr_quality.py -q
npm run test:ocr:browser
```

Workflow `.github/workflows/tests.yml` содержит быстрые frontend/gateway/Python проверки и отдельный тяжелый OCR quality job с `chi_sim+eng+rus`, Tesseract language packs и Noto CJK fonts.

## Выбор стратегий OCR (в UI)

Выбор OCR - это часть логики браузерного UI, а не отдельный сервис.

- **Auto**: если diagnostics уже видит offline backend, сразу выбирает browser OCR; иначе пробует `/api/convert` и при ошибке переключается на browser OCR.
- **Gateway / Local Tesseract / Local EasyOCR**: все идут через `/api/convert`; локальные режимы только добавляют `engine_type=tesseract|easyocr`.
- **Browser Engine**: полностью работает в браузере через PDF.js/Canvas и Tesseract.js WASM.
- **LLM Cloud API**: браузер напрямую вызывает Gemini или OpenRouter по ключу пользователя.

## Документация

Вторая часть документации (архитектура, задания курса, план и гайдлайны) была вынесена в отдельные файлы для поддержания чистоты:

- **[Архитектура и границы файлов](./docs/architecture.md)**
- **[План курса и что уже сделано](./docs/COURSE_PLAN.md)**
- **[Задания курса](./docs/course_tasks.md)**
- **[Общие требования к коду](./docs/code_guidelines.md)**
- **[План рефакторинга (`App.tsx`, `run.sh` и т.д.)](./docs/refactoring_plan.md)**
