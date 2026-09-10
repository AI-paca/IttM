# Rust language and bounded segment recursion

Base: `e1882c5a`.

## Diagnosed boundary

The universal `rust_separated_v1` route preserved the persistent language
agenda in native and browser OCR adapters, but discarded its quality result.
Rust planned a fixed list of blocks, `confidence_milli` was stored without
affecting the queue, and a bad 16-line block therefore reached
`generate-object` unchanged.

## Implemented experiment

- Native Tesseract/EasyOCR reuse their existing word recognition and language
  agenda; no second unconditional all-language pass was added.
- Browser Tesseract reuses its existing persistent worker, language
  probabilities, candidate ranking, and the shared grammar WASM assessment.
- Adapters return the existing grammar result through
  `confidence_milli`.
- Rust accepts blocks at 97% or above. A lower non-zero score splits only that
  block as `16 -> 8 -> 4 -> 2 -> 1`.
- Superseded parent text is excluded from final Markdown. Terminal children
  are rendered in source order.
- A score of zero remains “no quality evidence”, preserving non-OCR adapters
  that cannot supply grammar evidence.
- The queue remains bounded by `MAX_JOBS`; clean blocks cost one OCR call.

## Validation

| Gate                                      | Result                                                                      |
| ----------------------------------------- | --------------------------------------------------------------------------- |
| Rust unit and ABI                         | 20/20 pass                                                                  |
| Native/WASM parity verifier               | pass                                                                        |
| Browser pipeline and persistent worker    | 33/33 pass                                                                  |
| GitHub Pages/lite production bundle       | pass                                                                        |
| Native Python syntax/import in OCR image  | pass                                                                        |
| Shared production/debug recognizer import | pass                                                                        |
| Visual `SAMPLE_4k` separate-block smoke   | one non-empty bounded block; no background jobs                             |
| Three-fixture OCR smoke                   | pending until the uncontaminated baseline timing run releases OCR resources |
| Full corpus                               | pending; do not merge to clean before comparison                            |

## Baseline observation

The unchanged clean native-PDF phase completed in 440 seconds. Native text
PDFs retained all source segments, while raster scans already failed in the
OCR path: mixed sample was 52.07% for Tesseract and 41.12% for EasyOCR;
scanned-plan files ranged from 10.76% to 58.53%. This localizes the experiment
to `ocr-blocks -> get-segment`, not native PDF extraction.

## Rejected interpretation

The stale ABI test fixtures originally used confidence 900 as an ignored
placeholder. Under the new contract that correctly means 90% and requests a
split. Non-recursive parity fixtures now use 1000; a dedicated Rust test
covers the 960 split path.

## Debug route

`debug-rust-separated.py` now consumes the same shared recognizer callback
as the API route and follows the session's growing job count. Its JSON records
the grammar score of every parent and child block, so a low-quality split is
visible in the visual report rather than silently omitted.
