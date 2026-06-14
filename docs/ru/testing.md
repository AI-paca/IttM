# Тестирование

[English](../en/testing.md) | [Документация](./README.md)

## JavaScript / TypeScript

```bash
npm run format:check
npm run lint
npm test
npm run build
```

`npm run lint` включает ESLint и typecheck для web, gateway и edge.
`npm test` проверяет URL/ошибки API, частичный NDJSON, gateway proxy и
backpressure adapter, PDF workers, Base64-поток, consent и Tesseract assets.

### Contract и smoke tiers

Быстрый PR-safe contract tier:

```bash
npm run test:contract
```

Он запускает task lifecycle, worker protocol, bounded input storage, browser
layout contracts, generated fixture registry и быстрые upload/resource
инварианты. Все fixtures создаются детерминированно из кода и seed-значений;
внешний `testtables/` не нужен.

Smoke tier:

```bash
npm run test:smoke
```

Он проверяет HTTP routing/static glue и FastAPI endpoint wiring. Файлы
`gateway/src/core/routes.test.ts` и `ocr/tests/test_main.py` относятся к smoke:
они полезны для связности адаптеров, но не заменяют контракты `TaskService`,
worker protocol и focused service tests.

Отдельная регрессия локальных режимов требует, чтобы frontend передавал тот же
объект `File` в `FormData` без browser-side `arrayBuffer()`, а gateway
проксировал исходный `Request.body`.

## GitHub Pages

```bash
npm run build:pages
npm run test:pages
```

Проверка поднимает временный HTTP server и получает через base path `/IttM/`
локальный Tesseract worker и четыре worker/core asset. Это проверяет production
URL и код 200, но не заменяет multilingual OCR quality test. Команды нельзя
запускать параллельно с обычной сборкой, потому что обе записывают результат в
`dist`.

## Python OCR

```bash
docker build -f docker/ocr.Dockerfile --target test \
  --build-arg PYTHON_REQUIREMENTS=requirements-ci.txt \
  --build-arg OCR_INSTALL_CJK_FONTS=1 \
  -t ittm-ocr-ci ./ocr

docker run --rm ittm-ocr-ci python -m flake8 .
docker run --rm ittm-ocr-ci python -m black --check .
docker run --rm ittm-ocr-ci python -m ruff check .
docker run --rm ittm-ocr-ci python -m pytest tests -q
```

Строгие multilingual quality-тесты генерируют собственные fixtures и
проверяют `eng`, `rus` и `chi_sim` отдельно для browser Tesseract.js и backend
Tesseract, включая PDF без принудительного upscaling.

## Docker

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS "http://$(docker compose port nginx 80)/api/health"
```

Сборка образов требует доступного Docker DNS. Ошибка
`Temporary failure resolving deb.debian.org` относится к build network
окружения и должна перепроверяться в GitHub CI.

## Ручной corpus

`testtables/` и `testtables/tmp/` игнорируются Git. Это только ручной A/B
corpus: он не входит в `npm test`, `npm run test:contract`, smoke suite или
любой обязательный PR gate. Для PR-safe повторяемых прогонов используется
generated fixture registry из `ocr/tests/generated_media.py`.

Ручные A/B прогоны:

- `scripts/benchmark-testtables.sh`
- `scripts/benchmark-browser-testtables.sh`
- `scripts/benchmark-browser-pdf-memory.mjs`
