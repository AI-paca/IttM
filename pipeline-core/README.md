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

The C/WASM ABI exports:

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

Python `/readiness` reports `pipeline_core_abi5`. A missing or incompatible
native library makes readiness fail; production raster conversion does not
silently fall back to the old Python layout route.

When changing ABI behavior, update the Rust tests, native wrapper, browser
wrapper, parity verifier, WASM verifier, and this document together.
