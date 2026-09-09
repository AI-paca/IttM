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

| Fixture                           | Last good input                     | First broken stage | Evidence                                                                                                                     |
| --------------------------------- | ----------------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `doc_course_tasks_legacy.png`     | one correctly detected table object | `separate-block`   | one nearly full-table OCR crop was emitted instead of overlapping dyadic masks                                               |
| raster tables after compact masks | readable non-empty OCR blocks       | `get-segment`      | only plain block text crossed the ABI, so mask membership and word positions were unavailable and final text would duplicate |

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

## Update: isolated route audit (2026-08-31)

### Successful

- Explicit `best_int` tessdata is no longer replaced by the browser model
  downloader. On `image copy.png`, native and WASM now produce byte-identical
  final Markdown.
- Unavailable browser language profiles are skipped instead of silently
  substituting another profile.
- Grammar normalization preserves the source arrow notation used by the
  reference output.
- Native PDF object and segment discovery is now owned by Rust and is exposed
  through the same native/WASM ABI. The eight-page curriculum PDF produces 45
  objects and 5538 segments in the browser. Local `pdftotext` grouping produces
  5527 segments, with 9975/9975 final text tokens shared with the browser route.
- `doc_course_tasks_legacy.png` produces one paragraph object, one `8 x 6`
  table object and eight overlapping dyadic table blocks. Empty logical cells
  remain topology placeholders but are absent from the packed OCR rasters.
- Final raster objects are rendered by Rust `reading_index`, not by a terminal
  OCR job's local row. The standalone table header is therefore emitted before
  the table.
- The browser debug adapter now reads Rust objects, base blocks and dense block
  rasters directly from the existing ABI. Recursive language attempts are
  reported under `ocr-blocks`, not mislabeled as `separate-block` output.

### Rejected hypotheses and approaches

- Kornia is not a geometry or table-partition dependency. The autotune lab used
  Kornia as an Apache-2.0 evaluation reference; production Rust owns the small
  gamma formula and invokes `gamma-dark` only as an OCR retry. It can improve or
  worsen individual recognized words, but cannot change alignment, objects,
  block masks or native PDF topology.
- The current Python fixed-width PDF oracle is not a usable source of truth for
  object boundaries: it produced 113 objects and 3359 segments on the curriculum
  PDF, versus the Rust/PDF.js text-layer route's 45 objects and 5538 segments.
- Passing every `pdftotext` word as an independent segment destroys line
  context and creates 9979 tiny segments. Grouping XML words by the PDF line is
  both faster and structurally faithful.
- Bridging any adjacent wide PDF tables over paragraph-like rows overmerged
  unrelated small tables. The accepted bridge is restricted to very large,
  similarly wide tables with aligned interruption rows.

### Still unverified

- SCA/SBOM through the GitHub Actions workflow after the current adapter change.
- At least 60% final corpus quality for each declared engine and route.
- Full all-engine, browser, native PDF and raster comparison against the July 30
  checkpoint.
