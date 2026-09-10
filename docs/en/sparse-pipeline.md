# Sparse pipeline reference

`ocr/app/sparse_pipeline` is an opt-in page runtime. Public conversion routes
still call `ocr/app/services/convert_service.py`; no route constructs
`SparseConvertService`. Do not use sparse artifacts to describe a public API
response until that wiring and its integration tests exist.

`PIPELINE_ORDER` certifies semantic evidence in the order
`3 → 1 → 6 → 4 → 5 → 2 → 7`: bounded control, physical geometry, object
reconstruction, crop candidates, overlapping blocks, OCR evidence fusion, and
document assembly.

The current `SparsePipelineRuntime.process_page` dataflow has an intentional
dependency that must not be hidden by those labels. After Stage 6,
`OverlappingBlockPlanner` first calculates the Stage 5 memberships and
coordinates. `BlockCropper` then applies the Stage 4 RAW/GAMMA recipe to each
immutable block crop. The global Stage 4 artifact is absent in production
evidence (`stage4=None`). Stage 2 and Stage 7 follow. The pipeline diagram shows
this call order; `PIPELINE_ORDER` remains the certified evidence order.

Production Stage 1 requires
`coordinate_mode=pixel_partition`; logical projections belong to versioned
diagnostic adapters.

For incidents, use
[`scripts/debug/debug-all-separated.sh`](../../scripts/debug/debug-all-separated.sh).
Its directories `00-preprocess` through `07-generate-object` are persisted
debug boundaries, not the semantic stage numbers above. The complete tracked
table run, every matrix/object/block PNG, and the support commands live beside
the runner in [`debug/EXAMPLE.md`](../../debug/EXAMPLE.md).

Scripts containing `legacy`, a saved-version comparison, or `v<number>` are
frozen-contract labs, not support entry points.

The current executable invariants live in
[`ocr/tests/sparse_pipeline`](../../ocr/tests/sparse_pipeline). The RAW/GAMMA
crop provenance is described in [`gamma-recipe.md`](./gamma-recipe.md).
