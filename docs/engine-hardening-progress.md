# Engine Hardening Plan and Progress

Branch constraint: all work stays on `engine` or local descendants of `engine`.
`main` must not be checked out, rewritten, merged into, or committed to.

## Inputs

The implementation tracks the concerns from:

- `docs/ревью от LLM/user-junior-review.md`
- `docs/ревью от LLM/middle-review.md`
- `docs/ревью от LLM/senior-review.md`
- `docs/ревью от LLM/true-architecture.md`
- the user request from June 11, 2026

## Goals

| Area | Requested outcome | Verification | Status |
| --- | --- | --- | --- |
| GitHub Pages | Prevent another missing `BASE_URL` regression for OCR worker/core assets | Build-level test inspects the Pages bundle and files; browser smoke test runs OCR under `/IttM/` | In progress |
| Browser PDF | Keep expensive PDF page rendering and pixel scanning off the UI thread where browser support allows it | Worker unit tests, PDF regression tests, browser smoke test | Pending |
| Browser Base64 | Avoid main-thread `FileReader` and giant intermediate data URLs for images/PDF pages | Worker conversion tests and cancellation/error tests | Pending |
| Browser memory | Bound page/image dimensions and release canvases, bitmaps, object URLs and workers deterministically | Memory-oriented browser scenario and code-level cleanup tests | Pending |
| Backend uploads | Stop persisting ordinary image uploads to `/tmp`; keep bytes in memory with explicit size limits | FastAPI tests assert no temp file for images and cleanup for PDFs | Pending |
| Tesseract I/O | Feed PIL/numpy objects directly to `pytesseract`; document unavoidable subprocess internals | Engine tests with mocked pytesseract and temp-directory monitoring | Pending |
| EasyOCR fallback | Avoid selecting GPU EasyOCR when available VRAM is below a configurable threshold; recover from OOM | Resource policy unit tests and engine fallback tests | Pending |
| External LLM consent | Require explicit per-session consent before a document is sent to Gemini/OpenRouter | UI/context tests and request-blocking unit tests | Pending |
| Docker resources | Measure steady-state and request memory for browser/backend paths; detect leaked temporary files | Repeatable scripts/commands recorded below | Pending |

## Scope Decisions

### Implement in this series

1. Build-time and browser-level Pages regression protection.
2. Web Worker based PDF raster preparation and Base64 conversion with a
   compatibility fallback.
3. In-memory image upload handling and bounded PDF spooling in FastAPI.
4. Resource-aware EasyOCR selection and graceful fallback to Tesseract.
5. Explicit user consent before external LLM transmission.
6. Tests for every changed contract and failure mode.

### Deferred architecture project

The reviews correctly identify that synchronous OCR requests do not scale to
high load. Redis/RabbitMQ, S3/MinIO, task IDs, cancellation propagation and
presigned uploads should be designed together. Adding only one of those pieces
would increase operational complexity while leaving the transport contract
half synchronous. This series will leave a documented boundary and measurable
limits, not pretend that the local stack is already a distributed OCR service.

Direct Nginx-to-Python uploads are also deferred because the Gateway currently
owns API routing and error normalization. Removing it requires contract and
deployment changes across local, Docker, Pages and Edge modes.

## Commit Plan

1. `docs: track engine hardening work`
2. `test(web): guard GitHub Pages OCR asset paths`
3. `refactor(web): move document encoding work off main thread`
4. `refactor(ocr): process uploads in memory`
5. `feat(ocr): add resource-aware EasyOCR fallback`
6. `feat(web): require consent for external LLM OCR`
7. `test: verify OCR resource cleanup and limits`
8. `docs: record hardening results and remaining risks`

## Progress Log

### 2026-06-11

- Confirmed active branch is `engine` at `95b0bd1`; local `main` remains at
  `507625d`.
- Read all four LLM review drafts.
- Confirmed the previous Pages failure mechanism: indirect access to
  `import.meta.env.BASE_URL` compiled to `/`, so the browser requested
  `/vendor/tesseract/worker.min.js` instead of
  `/IttM/vendor/tesseract/worker.min.js`.
- Confirmed current PDF processing renders pages, scans pixels and serializes
  JPEG data on the UI thread.
- Confirmed image uploads are written by the FastAPI router before
  `convert_service` opens them again.

## Verification Record

Commands and measured results will be appended here after each implementation
step. Failed checks stay in the log with their cause; they are not erased.

## Remaining Risks

- Browser OCR language data still comes from an external CDN unless a local
  `langPath` is configured.
- `pytesseract` launches the Tesseract executable. Passing PIL images avoids
  application-managed temp files, but the library/subprocess may still use
  short-lived OS resources internally.
- PDF rasterization through Poppler may require bounded disk-backed spooling
  for large documents. The target is deterministic cleanup and limits, not an
  unsafe promise of zero disk use for every PDF size.
- High-load async queues and object storage remain a separate architecture
  milestone.
