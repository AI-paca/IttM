# OCR tests

- `api/` — FastAPI routes, uploads, PDF modes, and progress.
- `debug/` — manual-corpus tooling and diagnostic script regressions.
- `engines/` — OCR engines, preprocessing, and pipeline flags.
- `layout/` — chunking, geometry, tables, and Markdown reconstruction.
- `quality/` — generated fixtures, quality metrics, and OCR quality gates.
- `support/` — shared fixture generators, metrics, and mutation helpers.
- `fixtures/` — generated or tracked binary fixtures.

`conftest.py` and package initialization stay at the test root.
