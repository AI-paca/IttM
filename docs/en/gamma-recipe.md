# GAMMA candidate provenance

The sparse OCR pipeline generates an optional candidate with the fixed
`RGB -> grayscale -> gamma(1.2)` transform. The implementation is in
[`crop_enhancement.py`](../../ocr/app/sparse_pipeline/crop_enhancement.py)
under recipe ID `kornia-gamma-dark-v1`.

The mathematical recipe was evaluated against Kornia, which is distributed
under Apache-2.0. IttM does not copy Kornia source and does not require Kornia
at runtime: the default implementation is a clean NumPy expression of the same
equation. An optional Torch/CUDA backend implements the identical byte-level
contract.

The transform is a candidate generator, not a replacement for the source
image. Every block retains its RAW crop; RAW and GAMMA observations are
compared later by OCR evidence fusion. This is required because the measured
corpus contained both improvements and regressions from gamma preprocessing.

Long-term invariants:

- `GAMMA_DARK == 1.2`;
- the result is an immutable grayscale PNG with the same geometry and DPI;
- `source_sha256` binds the candidate to its RAW crop;
- NumPy and Torch/CUDA backends produce the same pixels;
- failure to generate GAMMA must not alter RAW evidence.

The executable provenance check is
[`test_crop_enhancement_stage4.py`](../../ocr/tests/sparse_pipeline/test_crop_enhancement_stage4.py).
Any recipe or backend change must update that test and this file.
