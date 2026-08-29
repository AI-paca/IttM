# Universal Rust route status (2026-08-26)

## Route contract

Raster inputs use one stage owner in every runtime:

`preprocess -> geometry -> topology -> find-object -> separate-block -> ocr-blocks -> get-segment -> generate-object`

- Rust pipeline ABI: `6`
- Rust route id: `0x52530003`
- Native CLI/API: `NativeSeparatedSession` calls this ABI.
- Browser and lite HTML: `BrowserSeparatedSession` calls the same WASM ABI.
- PDF text layer is the intentional exception: it has native PDF object and
  segment discovery, then hands segments to the shared structural assembler.

No second table-first or Python structural route is selected by the production
raster conversion service.

## Confirmed failures before this experiment

| Fixture | Last good input | First broken stage | Evidence |
| --- | --- | --- | --- |
| `doc_course_tasks_legacy.png` | one correctly detected table object | `separate-block` | one nearly full-table OCR crop was emitted instead of overlapping dyadic masks |
| raster tables after compact masks | readable non-empty OCR blocks | `get-segment` | only plain block text crossed the ABI, so mask membership and word positions were unavailable and final text would duplicate |

## Implemented

- Table header stays a standalone block.
- Data cells are emitted as bounded `16 x 16` tiles with full, row-bit and
  column-bit membership masks.
- Empty source cells are excluded before compact raster generation.
- Glyphs preserve source pixel size.
- Native and browser OCR return observed word boxes through one ABI.
- Rust maps each word to its compact slot and therefore to the original table
  coordinate.
- Repeated mask observations are deduplicated by exact text consensus and
  confidence.
- Logical table dimensions retain empty placeholders.
- Engines without word boxes retain their previous text-only output rather than
  producing an empty table.
- Low grammar quality remains a bounded binary split; clean blocks do not fan
  out into an N*M language sweep.

## Language path

The existing language agenda was not rewritten.

- Tesseract keeps a persistent `LanguageAgenda` and reorders candidates from
  observed document evidence.
- The first unresolved block can sweep available language profiles.
- A result below 97% causes Rust to split only that block.
- Browser uses one cached multilingual worker and exposes word boxes.
- Kornia gamma-dark remains an OCR-attempt transform, not a geometry transform.

## Validation

Successful:

- Rust tests: `21/21`
- Native Python/Rust parity
- WASM export and route verification
- TypeScript typecheck
- Browser pipeline and persistent worker tests: `33/33`
- GitHub Pages/lite production build
- Direct Rust test maps two OCR word boxes back to two source cells

Pending:

- GitHub Pages artifact verifier also requires the unrelated SmolLM2 reviewer
  asset, which is not present in any local workspace. The lite application and
  ABI build succeeded; the complete Pages packaging gate is not yet green.
- Three-fixture native/browser OCR smoke waits for the baseline corpus timing to
  finish so it cannot distort that comparison.
- The corrected branch still needs a full corpus run after the baseline
  `clean` and checkpoint runs.
