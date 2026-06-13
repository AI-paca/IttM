# Testing

[Русский](../ru/testing.md) | [Documentation](./README.md)

## JavaScript / TypeScript

```bash
npm run format:check
npm run lint
npm test
npm run build
```

The test suite covers API errors and URLs, NDJSON parsing, gateway proxying,
PDF workers, Base64 streaming, external LLM consent, and Tesseract asset paths.

A dedicated local-mode regression requires the frontend to place the original
`File` into `FormData` without browser-side `arrayBuffer()`, while the gateway
must forward the original `Request.body`.

## GitHub Pages

```bash
npm run build:pages
npm run test:pages
```

The verifier checks `/IttM/`, the local Tesseract worker, and four worker/core
assets. Do not run the normal and Pages builds concurrently because both write
to `dist`.

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

Strict multilingual quality tests generate their own fixtures and verify
`eng`, `rus`, and `chi_sim` with browser Tesseract.js and backend Tesseract.

## Docker

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS "http://$(docker compose port nginx 80)/api/health"
```

Image builds require working Docker DNS. A
`Temporary failure resolving deb.debian.org` error belongs to the build
environment and must be rechecked in GitHub CI.

## Manual Corpus

`testtables/` is ignored and serves only as an A/B corpus. Reusable runners:

- `scripts/benchmark-testtables.sh`
- `scripts/benchmark-browser-testtables.sh`
- `scripts/benchmark-browser-pdf-memory.mjs`
