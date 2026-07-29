# v17: shared fallback and overlap decisions

## Runtime contract

The deterministic fallback decisions live in `pipeline-core` ABI 3:

- `ittm_should_replace_primary` accepts observed character and retained-token
  evidence and rejects a lossy full-page replacement;
- `ittm_should_drop_text_block` removes an incoming repeated block when its
  normalized text or token coverage proves that it is already represented.

Python does not copy these thresholds. Local and Docker backends load the
release native library through `ITTM_PIPELINE_CORE_LIB`; readiness fails when
the configured library is missing or has the wrong ABI. GitHub Pages Lite
builds the same crate for `wasm32-unknown-unknown`. A backend web build neither
builds nor selects browser OCR; browser fallback is enabled only by the
explicit Lite build mode.

```text
pipeline-core (Rust ABI 3)
├── local / Docker backend -> libittm_pipeline_core.so -> Python OCR adapter
└── GitHub Pages Lite      -> ittm_pipeline_core.wasm  -> browser OCR adapter
```

OCR itself remains a platform port (Tesseract/EasyOCR in Python and
Tesseract.js in the browser). Both ports derive Unicode token evidence, while
Rust owns the replacement and duplicate thresholds.

## Targeted regression results

No full benchmark was run.

- `photo_6_2026-05-12_22-26-36.jpg`, Tesseract: replacement evidence was
  `primary_chars=110`, `fallback_chars=205`, retained long/numeric tokens
  `12/12`. The shared core correctly allowed the non-lossy expansion. Current
  success is `43.07`; the remaining gap to v9 is upstream recognition and
  segmentation, not fallback replacement. The old v9 result also included
  removed canonical/canned recovery and is not a valid implementation target.
- `photo_10_2026-05-12_22-26-36.jpg`, EasyOCR: repeated title count changed
  from `2` to `1` in a real conversion. Current success is `55.47` versus the
  saved v16 value around `54.2`. The remaining v9 gap is structural
  classification (the old header table), not repeated-block merging.

The changes intentionally do not recreate fixture documents or canonical
labels. Further work on `photo_6` must improve observed region OCR; further
work on `photo_10` must classify the heading/table structure from geometry.
