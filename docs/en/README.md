# IttM Documentation

<p align="right">
  <a href="../../README.md"><img alt="Русский" src="https://img.shields.io/badge/%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9-%F0%9F%87%B7%F0%9F%87%BA-blue"></a>
  <a href="./README.md"><img alt="English" src="https://img.shields.io/badge/English-%F0%9F%87%AC%F0%9F%87%A7-lightgrey"></a>
</p>

[Root README](../../README.md) | [Русский](../ru/README.md)

The user-facing intro lives in the [root `README.md`](../../README.md). This
index collects technical documentation for developers and contributors:
architecture, contracts, limits, tests, project vision.

## Documentation map

| Document                                                                   | What it covers                                                                   |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| [Architecture](../ru/architecture.md)                                      | Runtime topology (local/Docker), shared contract, Mermaid flow.                  |
| [Unified pipeline: target model](../ru/architecture-unified-pipeline.md)   | Target: one contract, one flag resolver, one PDF contract.                       |
| [Current flag/profile implementation](../ru/architecture-current-flags.md) | What's in the code: `OcrPipelineProfile`, `pipeline_flags`, `pdf_mode`, engines. |
| [OCR architecture limits](../ru/architecture-limitations.md)               | Hard memory / PDF / table limits.                                                |
| [Project vision](../ru/roadmap/vision.md)                                  | Browser extension, Linux long-screenshot pipeline, marketplace-cart whitelist.   |
| [Engine and profiles](../ru/engine/README.md)                              | Backend profiles, pipeline flags, doc-verifier contract.                         |
| [Testing](../ru/testing.md)                                                | Test tiers, profile selection oracle, PR gate.                                   |
| [Debug](../ru/debug.md)                                                    | Local reproducible OCR inputs and artifacts.                                     |
| [Security policy](../ru/security.md)                                       | Trust boundaries, open risks, threat model.                                      |
| [SAST](../ru/sast.md)                                                      | Semgrep gate, scope, CI artifacts, and Hw6 boundaries.                           |
| [Manual Docker launch](../ru/docker-manual-launch.md)                      | `docker build` / `docker run` without Compose.                                   |
| [Responsibility boundaries](../ru/course/boundaries.md)                    | Entry points and file ownership by component.                                    |
| [Tesseract quality experiment](../ru/experiments/tesseract-quality.md)     | Why an oracle is needed and what artifacts were collected.                       |
| [Roadmap history](../ru/roadmap/history.md)                                | How the architecture reached its current shape.                                  |
| [Development branches](../ru/roadmap/development-branches.md)              | Actual branches, active lines, archives.                                         |
| [Course task criteria](../ru/course/course_tasks.md)                       | Course tasks mapped to the implementation.                                       |

## Engines

| Engine          | Where it runs                  | Source document transfer                          |
| --------------- | ------------------------------ | ------------------------------------------------- |
| Local Tesseract | Python FastAPI (backend)       | multipart, no browser-side `arrayBuffer()`/Base64 |
| Local EasyOCR   | Python FastAPI (backend)       | multipart, no browser-side `arrayBuffer()`/Base64 |
| Browser OCR     | Tesseract.js worker in browser | file never leaves the tab                         |
| External LLM    | selected provider API          | only after explicit user consent                  |

## Shared contract

One set of routes for Web UI, CLI and `curl`. Response format follows the
`Accept` header: `text/plain`, `text/markdown`, `application/json`,
`text/event-stream`, `application/x-ndjson`.

| Route                                                                | Method   | Purpose                                                               |
| -------------------------------------------------------------------- | -------- | --------------------------------------------------------------------- |
| `/api/extract/text`                                                  | POST     | Synchronous extraction.                                               |
| `/api/tasks`                                                         | POST/GET | Async tasks: `queued → running → ... → cancelled/partial/complete`.   |
| `/api/tasks/:id`                                                     | GET      | Status and result.                                                    |
| `/api/tasks/:id/events`                                              | GET      | SSE progress stream; resume via `Last-Event-ID`.                      |
| `/api/tasks/:id/cancel`                                              | POST     | Cancel.                                                               |
| `/convert`, `/convert/stream`                                        | POST     | Legacy-compatible OCR routes.                                         |
| `/api/health`, `/api/capabilities`, `/api/diagnostics`, `/api/probe` | GET/POST | Runtime state, limits, dry-run.                                       |
| `/v1/pipeline/flags`                                                 | GET      | Catalog of effective flag keys (shared across backend, browser, LLM). |
| `/api/install-easyocr` (+`/status`)                                  | POST/GET | EasyOCR install and status.                                           |

`pdf_mode=auto|raster` is accepted in query (`?pdf_mode=...`), header
(`X-PDF-Mode`), JSON field (`pdfMode`) and CLI flag (`--pdf-mode`). Unknown
values → HTTP 400. The actually-used mode is reported back in
`meta.pdf_mode`.

In-memory task queue: `maxWorkers: 1`, `maxQueued: 32`. Tasks live in the
gateway process memory and do not survive a restart; there is no durable
queue, no retry, no retention.

## Launch

```bash
# Full version (Web UI + backend)
bash scripts/runtime/run-local.sh

# Static Web UI without backend OCR
bash scripts/runtime/build-lite.sh

# Docker Compose (Web UI + backend)
docker compose up -d && docker compose port nginx 80
```

Detailed requirements and commands without Compose live in
[docker-manual-launch.md](../ru/docker-manual-launch.md).

## Checks

```bash
npm run format:check
npm run lint
npm test
npm run test:contract
npm run test:smoke
npm run build
npm run build:pages && npm run test:pages
docker compose config --quiet
```

Python checks (flake8 / Black / Ruff / pytest) and OCR tiers are described in
[testing.md](../ru/testing.md).
