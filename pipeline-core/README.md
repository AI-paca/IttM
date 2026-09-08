# IttM pipeline core

`pipeline-core` is the single Rust source for the separated raster route and
bounded deterministic decisions shared by the Python backend and browser. The
same crate is compiled to `libittm_pipeline_core.so` for Python and
`ittm_pipeline_core.wasm` for the browser; there is no second copy of those
Rust decisions.

Image/PDF decoding and the OCR engines remain platform-specific adapters. Rust
owns the production raster stages, block geometry/order, and final assembly.
The only production bypass is a trustworthy native PDF text layer unless the
caller forces raster mode.

## ABI v6

Key C/WASM ABI exports include:

- `ittm_pipeline_abi_version`;
- `ittm_pipeline_route_id` (the same deterministic route marker in native and WASM builds);
- `ittm_pipeline_recipe_mask`;
- `ittm_sparse_add_signal`;
- `ittm_is_isolated_heading`;
- `ittm_span_evidence_score`;
- `ittm_should_replace_primary`;
- `ittm_should_drop_text_block`;
- `ittm_alloc` / `ittm_dealloc`;
- `ittm_separated_begin` / `ittm_separated_drop`;
- `ittm_separated_job_count` / `ittm_separated_job_field`;
- `ittm_separated_set_ocr`;
- `ittm_separated_add_ocr_word`;
- `ittm_separated_add_ocr_word_ppm`;
- `ittm_separated_render_length` / `ittm_separated_render_copy`;
- `ittm_separated_stage_mask`.

The separated session executes `preprocess → geometry → topology → find-object
→ separate-block → ocr-blocks → get-segment → generate-object`. Raster bytes
and UTF-8 OCR results cross the ABI; platform engine objects do not.

Python loads the native library through `ocr/app/pipeline_core/native.py`.
Browser code loads the WASM module through
`web/src/ocr/pipeline-core.ts`. The files under `web/src/wasm/ocr-core` belong
to a separate generated grammar module; its wrapper exists, but the current
production browser path does not call it.

## Build and verification

```bash
npm run build:pipeline-core
```

This runs Rust tests, builds the native release library, checks Python/Rust
parity, builds `wasm32-unknown-unknown`, and verifies the exported ABI in Node.

Individual builds:

```bash
npm run build:pipeline-core:native
npm run build:pipeline-core:wasm
```

Generated native files under `pipeline-core/target` and
`web/public/wasm/ittm_pipeline_core.wasm` are ignored. Docker OCR builds compile
and copy the native library into the runtime image; Lite builds generate the
WASM module.

Python `/readiness` reports `pipeline_core_abi6`. A missing or incompatible
native library makes readiness fail; production raster conversion does not
silently fall back to the old Python layout route.

When changing ABI behavior, update the Rust tests, native wrapper, browser
wrapper, parity verifier, WASM verifier, and this document together.


### Frozen OCR stage inputs

`export-python-ocr-checkpoint.py` exports the selected legacy Python OCR plan,
words and complete matrix without executing OCR or reading recognized stage-06
text. Selection metadata is an explicit input to this materialization check;
it does not prove policy-selection parity.

The block import accepts the original packed-u32 metadata version 1. Version 2
appends a logical cell count followed by each cell's row, column, row/column
span, source rectangle and a length-prefixed list of geometric source IDs.
Empty source lists are valid. Version 3 appends a text-order mode (0 retains
OCR text; 1 requests Python line ordering for a single membership unit).

`ittm_separated_import_block_geometry` imports the same metadata and raster
dimensions without loading pixels. It is for Python-05 → Rust-06 replay; it
refuses to start OCR or import raster-coordinate OCR results. Raster-backed
import remains available for earlier boundaries. Composite imported OCR
results use transform code 2 and preserve Python's original crop extent and
source-block offset interpretation. No word coordinates are clipped or
replaced by geometry IDs. These entry points are compiled for native and WASM
from the same Rust source.

### Object boundary parity

Object reconstruction now retains distinct crop and logical matrix rectangles,
rule-lattice versus topology-slice provenance, and the `flow` kind. The staged
ABI uses kind 4 for flow; its existing kinds 0–3 retain their meaning. The older
packed diagnostic API retains its four-category encoding for compatibility.
Finite mixed-axis cycles require both observed merge directions and independent
rule-network evidence. Empty corridor chambers precede semantic classification;
fragmented and stacked tables are joined using the same geometric witnesses as
the frozen Python route. Source segments remain separately accounted, including
structural edge residuals. Object-boundary agreement does not by itself prove
local matrix cell construction, OCR block raster construction or OCR selection.


Local matrices are now constructed in the shared Rust object stage from the
page topology, rule lattice and original geometric source IDs. The complete
row/cell representation preserves source-free empty cells, disconnected row
intervals and conservative observed payload identity. Its logical spans feed
the existing block planner; table OCR materialization receives all matrix
cells, including empty edge cells that have no OCR job. Residual geometry keeps
its source ownership but is excluded from OCR planning using Python's object
extent and structural-residual rules. No ABI version change is required.

The isolated matrix comparison covers 374 objects and 129937 cells from all
28 frozen Python documents in native Rust and real Chromium/WASM. It compares
every row interval and cell rectangle, topology code, and ordered source ID
list. The diagnostic input contains saved Python03 objects and literal page
topology, independently of Rust object reconstruction. Production integration
retains the earlier 28/28 topology and object metadata agreement. OCR block
planning, raster packing and language selection still require their own
isolated comparisons; local-matrix agreement does not establish full-route
quality.
