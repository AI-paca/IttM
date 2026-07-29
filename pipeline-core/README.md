# IttM pipeline core

This crate is the language-neutral deterministic core shared by the Python
backend and the browser-lite pipeline. Platform adapters own PDF decoding,
Pillow/Canvas images, native Tesseract, Tesseract.js, and remote OCR/VLM calls.

ABI v3 defines:

```text
ImagePlane
  -> Segment[]
  -> SparseProjection
  -> RecognizedSegment[]
  -> StructuralRecord[]
  -> StructuralRenderArtifact
```

Stage traces never contain pixel buffers or platform handles. `ImagePlane`
crosses only the adapter/core boundary. Capability recipes decide which stages
are executed; a trusted API result cannot accidentally enable local T9 or
lexical correction.

The raw ABI also exposes deterministic structural predicates. The first one,
`ittm_is_isolated_heading`, classifies a short sparse-row run from geometry and
content length only; it does not inspect OCR casing or fixture vocabulary.

`ittm_span_evidence_score` ranks numeric evidence for an already observed OCR
span. Text, language identifiers and document-specific schemas never cross
this scalar ABI, so the shared core cannot synthesize replacement content.

`ittm_should_replace_primary` and `ittm_should_drop_text_block` own the
fallback evidence thresholds used by both runtime adapters. Python loads the
native library, while the Lite build loads the same crate as WebAssembly.

Build and verify native and browser artifacts with one command:

```bash
npm run build:pipeline-core
```

The command runs Rust tests and Python/Rust native parity, builds
`wasm32-unknown-unknown`, then instantiates the raw module in Node and checks
its exported ABI. Native-only and Lite-only builds are available as
`build:pipeline-core:native` and `build:pipeline-core:wasm`. The generated
`.wasm` is a build artifact and is not tracked.
