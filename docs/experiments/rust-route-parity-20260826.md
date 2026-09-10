# Rust route parity, 2026-08-26

## Confirmed split

`scripts/debug/debug-all-separated.sh` historically executed the legacy Python
sparse pipeline. Production native/API conversion and browser conversion execute
`pipeline-core/src/separated.rs`. The old visual report therefore cannot prove
the current Rust route.

## Runtime names

- `legacy-python`: old stage-injectable reference pipeline.
- `rust-native`: production Rust shared library plus the native OCR adapter.
- `rust-wasm-node`: the browser WASM adapter under Node Canvas; it is not a real browser.
- Real lite HTML browser execution remains a separate Pages/browser gate.

Every Rust visual run records `route_id`. Native and WASM are comparable only
when this ID is identical.

## Current observable defects

- Rust geometry and topology are marked complete internally but are not exported
  by ABI v5, so visual debug reports must label them `opaque`, not `PASS`.
- Rust object kinds are exported by ABI v5 and are included in object PNG names.
- Current Rust `get-segment` effectively maps one OCR job to one segment.
- Current Rust block planning groups row bands in chunks of at most 16 lines; it
  does not implement the legacy matrix/OR-XOR table block planner.
- Rust debug can resume completed items, but arbitrary stage artifact injection
  is still available only in the legacy Python reference route.

## Confirmed improvements

- Native and WASM use route `0x52530002` and produce identical object/job plans
  on the three route-parity fixtures.
- The 55-file `find-object` corpus completes 55/55. Detached page-edge noise is
  suppressed while table regions touching page edges remain protected.
- `Adobe Scan Jun 20` page 3 is reduced from 24 noise objects to two real page
  objects. The decorative wave in `image (8).png` is removed without clipping
  the underlined URL.
- Sparse pages no longer merge all distant lines through an infinite gap.
- Rust debug resume reuses complete target-stage items and recomputes the first
  incomplete item from its source.
- Pages and local lite builds ship the same pinned `tessdata_fast` models for
  `eng`, `rus`, and `chi_sim`; the browser no longer silently downloads a
  different OCR model.
- A high-confidence mixed-script primary candidate is no longer replaced by a
  lower-confidence single-language retry merely because it contains valid
  one-character Cyrillic, CJK, or table tokens.

## Rejected approaches

- A line-ink threshold of 12 deleted a valid table row.
- Long-thin thickness ratios of 3 and 2 either destroyed an underlined URL or
  retained the decorative wave. Connected-component topology replaced them.
- Comparing Python snake_case route JSON directly with TypeScript camelCase
  produced a false parity failure; normalized fields match.

## Visual smoke baseline

- `SAMPLE_4k.png`: one paragraph/object/block; native, WASM Node, and a real
  Pages Chromium run return exactly `SAMPLE`.
- mixed RU/EN/ZH table: 12 row-shaped objects and 12 row blocks instead of one
  table object with matrix blocks. Browser CJK survives, but generated Markdown
  is still plain rows rather than a table.
- `image copy.png`: content objects are separated, but the metric table is still
  rendered as plain rows and the upper paragraph is incomplete.
- `photo_10_2026-05-12_22-26-36.jpg`: `00-preprocess/raster.png` remains strongly
  projective. Its first bad stage is geometry/preprocess; downstream objects do
  not identify the root cause.
- Native and WASM route IDs and every job bbox are identical. The first
  remaining structural failure for tables is `separate-block`, followed by the
  one-job/one-segment handoff. Geometry/topology remain opaque in debug output.

## Gates run

- Rust tests: 19/19.
- Browser worker tests: 23/23.
- Strict browser OCR: English, Russian, Chinese, and mixed line all exact.
- Pages bundle: ABI v5, shared Rust WASM, seven Tesseract assets, seven PDF.js
  decoder assets, and the local reviewer model pass the HTTP asset gate.
- Real headless Chromium under `/IttM/`: multilingual fixture and `SAMPLE_4k`
  complete without failed requests.
