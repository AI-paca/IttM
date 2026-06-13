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
`npm test` проверяет URL/ошибки API, NDJSON, gateway proxy, PDF workers,
Base64-поток, consent и Tesseract assets.

Отдельная регрессия локальных режимов требует, чтобы frontend передавал тот же
объект `File` в `FormData` без browser-side `arrayBuffer()`, а gateway
проксировал исходный `Request.body`.

## GitHub Pages

```bash
npm run build:pages
npm run test:pages
```

Проверка подтверждает base path `/IttM/`, локальный Tesseract worker и четыре
worker/core asset. Эти команды нельзя запускать параллельно с обычной сборкой,
потому что обе записывают результат в `dist`.

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
Tesseract.

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

`testtables/` и `testtables/tmp/` игнорируются Git. Это A/B corpus, а не
полное покрытие входных данных. Для повторяемых прогонов используются:

- `scripts/benchmark-testtables.sh`
- `scripts/benchmark-browser-testtables.sh`
- `scripts/benchmark-browser-pdf-memory.mjs`
