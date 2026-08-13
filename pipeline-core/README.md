# IttM pipeline core

`pipeline-core` is the single Rust source for the logical stage recipe and
bounded deterministic decisions shared by the Python backend and browser. The
same crate is compiled to `libittm_pipeline_core.so` for Python and
`ittm_pipeline_core.wasm` for the browser; there is no second copy of those
Rust decisions.

This does not mean that all OCR code is one Rust implementation. Image/PDF
decoding, Tesseract/EasyOCR/Tesseract.js, workers, and stage handlers remain
platform-specific executors behind the shared contract.

## ABI v3

The C/WASM ABI exports:

- `ittm_pipeline_abi_version`;
- `ittm_pipeline_recipe_mask`;
- `ittm_sparse_add_signal`;
- `ittm_is_isolated_heading`;
- `ittm_span_evidence_score`;
- `ittm_should_replace_primary`;
- `ittm_should_drop_text_block`.

The functions operate on capabilities, bounded numeric evidence, and sparse
codes. Text and platform handles do not cross the raw ABI.

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

Python `/readiness` reports `pipeline_core_abi3`. A missing or incompatible
native library makes readiness fail even though some Python helpers retain
fallback implementations for development and tests.

When changing ABI behavior, update the Rust tests, native wrapper, browser
wrapper, parity verifier, WASM verifier, and this document together.
