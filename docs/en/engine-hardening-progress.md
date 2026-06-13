# Engine Hardening Progress

Branch rule: all work is local and descends from `engine`. `main` remains
untouched.

## Stable Scope

| Area                     | Result                                                                                | Verification                         |
| ------------------------ | ------------------------------------------------------------------------------------- | ------------------------------------ |
| GitHub Pages assets      | Tesseract worker/core URLs include the Vite base path                                 | Pages production build verifier      |
| Backend documents        | Long false-table inputs are segmented; uploads no longer block the FastAPI event loop | Backend routing and processing tests |
| Browser encoding         | PDF preparation and Base64 work run in dedicated workers where supported              | Worker, cancellation and error tests |
| External LLM privacy     | Gemini/OpenRouter requests require explicit per-session consent                       | Request-blocking tests               |
| PDF worker               | PDF.js uses `OffscreenCanvas` and DOM-free worker factories                           | Worker platform tests                |
| Backend result streaming | Python, gateway and web expose page NDJSON incrementally                              | Route, parser and truncation tests   |
| Benchmarking             | Backend, browser image and browser PDF-memory runners are reusable                    | Scripts committed separately         |

## Deliberately Excluded

- Experimental aligned-row/table reconstruction shared by browser and Python.
- A hard 6 GiB VRAM threshold for EasyOCR.
- Forced 6000-pixel PDF rendering.
- A blanket table-cell cap that damages curriculum tables.
- A process-wide OCR semaphore.

These changes are not hidden inside the PR-ready branch. OCR quality and speed
experiments continue on separate descendants so each change can be compared and
reverted independently.

## Verification

Recorded on June 12, 2026:

- Frontend and gateway: 43 tests passed.
- Backend: 43 passed, 29 skipped.
- TypeScript typecheck passed.
- GitHub Pages build verified `/IttM/vendor/tesseract/worker.min.js` and four
  local Tesseract assets.
- Black and Ruff passed.

## Remaining Risks

- The Python route still assembles the complete upload in memory before OCR.
- Browser PDF initialization still reads the complete file into a worker
  `ArrayBuffer`.
- Streaming errors after response start are NDJSON `error` events with HTTP
  status 200.
- Tesseract/EasyOCR work remains synchronous CPU/GPU work inside worker threads;
  there is no distributed queue or cancellation propagation.
- Browser OCR language data can still use an external CDN unless `langPath` is
  configured locally.
