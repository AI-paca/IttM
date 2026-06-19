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
`eng`, `rus`, and `chi_sim` with browser Tesseract.js. Backend Tesseract also
ships `kaz` and `kir` traineddata for scanned Cyrillic PDF fixtures.

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

## Manual Debug

`debug/` is the tracked manual debug corpus. Inputs live under
`debug/fixtures/name.ext`, while hand-checked Markdown oracles live
under `debug/reference/name.ext.md`. Only the two SAMPLE inputs are tracked;
real local fixtures stay ignored. Runtime A/B output stays
under ignored `debug/tmp/`, while final matrices are written to
`debug/result.csv` and `debug/time.csv`. Legacy `testtables/` inputs are only a
fallback for old local worktrees.
`ocr/tests/debug/test_sample_corpus.py` runs the tracked SAMPLE fixtures through
backend Tesseract: the 4K edge-to-edge word sample and the hard image-only
10x14 mixed-script table PDF must both stay above the debug gate.

Reusable runners:

- `scripts/debug/debug-all.sh`
- `scripts/debug/run-debug.sh`
- `scripts/debug/run-browser-debug.sh`
- `scripts/benchmark/benchmark-browser-testtables.sh`
- `scripts/benchmark/benchmark-browser-pdf-memory.mjs`

The browser image benchmark executes the UI preprocessing path through a Node
Canvas shim. Dense-grid curriculum pages are intentionally slow multi-pass
quality probes, not PR-safe unit tests.

Backend debug runs can limit PDF pages with `--pages '1,3,5-7'` or with
per-file rules such as `--fixture-pages 'Adobe*.pdf=1-5'`. The older
`--max-pages N` form remains a shorthand for `--pages '1-N'`.

When a result contains Markdown tables, the backend runner writes one
`<fixture>.tables.md` snapshot under `debug/tmp/tables/<method>/`. All detected
table blocks are combined in that file instead of creating hundreds of tiny
files. Cells are separated with `|`; missing cells stay as empty placeholders.
If a method does not emit a Markdown table but manual `expected` contains one,
the runner writes a diagnostic expected-shaped Markdown table with only
OCR-confirmed cells filled. For table fixtures, treat 87% `match_percent` per
method as the minimum passing signal, 93% as good, and 97% as the target.
