# PR Review Brief: Engine Hardening

## Review Range

- Base: `9bcf6b8` - `test(web): guard GitHub Pages OCR asset paths`
- Target: local `engine`
- Remote operations: none
- `main`: not modified

## Commit Boundaries

1. `refactor(ocr): bound backend document processing`
   - Owns Python upload reading, long-image segmentation and page-at-a-time PDF
     rendering.
   - Revert when investigating backend upload/PDF regressions.
2. `refactor(web): isolate document encoding workers`
   - Owns browser PDF/image preparation, Base64 workers and external LLM
     consent.
   - Does not change Python OCR quality.
3. `fix(ocr): exclude probe stub from auto fallback`
   - Prevents diagnostic stub text from being treated as OCR success.
4. `fix(web): render PDF pages with worker-safe factories`
   - Removes DOM factory access from the custom PDF worker.
5. `chore(ocr): log PDF page progress`
   - Adds page numbers to backend logs without changing recognition.
6. `feat(ocr): stream backend page results`
   - Adds the Python iterator, NDJSON endpoints, gateway proxy and web consumer
     as one end-to-end contract.
7. `test: add repeatable OCR benchmark runners`
   - Adds measurement tools only; no runtime behavior.
8. `docs: describe stable engine boundaries`
   - Records actual limits and the excluded experimental scope.

## Why The History Was Rebuilt

The previous `engine-fix` line mixed experiments, corrections and partial
reverts. This branch reapplies final stable behavior directly on top of the
Pages guard. Runtime responsibilities are separated so a regression can be
bisected and reverted without removing unrelated fixes.

## Excluded From This PR

- `aligned_rows` and diagram-to-table reconstruction experiments.
- Cross-runtime attempts to mirror Python filters in Tesseract.js.
- Table-grid confidence changes that altered local Tesseract output.
- Coarse EasyOCR VRAM policy and global request serialization.

Local Tesseract quality is therefore not claimed to be solved by this branch.
It is the subject of a separate child branch with per-engine A/B artifacts.

## Review Findings

No release-blocking defect was found by the local test and static-check pass.
The important residual risks are:

- FastAPI still holds the complete uploaded file in `bytes`.
- Browser PDF still holds a complete worker `ArrayBuffer`.
- A streaming failure after headers are sent is represented by an NDJSON
  `error` event rather than a non-200 HTTP status.
- Browser and Python OCR are intentionally separate implementations; profile
  parity must be tested behaviorally, not assumed from shared names.

An independent subagent review was requested during development, but the review
service returned no report and then hit its usage limit. This file therefore
does not claim an independent LLM approval; it is the prepared review brief and
local audit record for the future PR.

## Verification

- `npm test`: 43 passed.
- `npm run typecheck`: passed.
- `npm run build:pages && npm run test:pages`: passed.
- Backend pytest: 36 passed, 7 skipped.
- Black: passed.
- Ruff: passed.
