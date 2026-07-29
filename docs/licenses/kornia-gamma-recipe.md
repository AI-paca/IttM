# Kornia gamma-dark recipe provenance

Stage 4 uses the fixed mathematical transform `RGB -> grayscale -> gamma(1.2)`.
It was selected from the local OCR autotune lab's comparable 600-image report;
the clean engine implementation does not copy the experimental v12 pipeline and
does not require Kornia at runtime.

This is an **optional candidate generator**, not an unconditional preprocessing
default. The raw crop remains available. Stage 5 carries raw and gamma variants
forward, and Stage 2 performs candidate recognition/selection from observed
evidence.

The measured upstream project is Kornia, distributed under Apache-2.0. That
license permits use in this educational project. The implementation in
`ocr/app/sparse_pipeline/crop_enhancement.py` is a clean-room NumPy expression
of the published equation. An optional explicit Torch/CUDA backend is isolated
behind the same byte-deterministic result contract.

Selection evidence recorded on 2026-07-19:

- report: `IttM-ocr-autotune-lab/docs/reports/preprocessors-universal-full600.json`;
- report SHA-256:
  `6e78679a920ba87416834a5966eb75dfd483653e59cc454779240f612716bb1f`;
- Kornia gamma-dark loss: 19,617 / 119,900 characters, 353 fewer edits than raw;
- only 265 / 600 pages improved, 92 tied and 243 worsened; mixed-language,
  JPEG, noise, tight-crop and several font-size strata regressed;
- therefore aggregate improvement alone is explicitly insufficient to discard
  the raw candidate;
- OpenCV CLAHE loss: 20,047; G'MIC local-normalize loss: 20,044;
- DocDiff was excluded as primary evidence because it had only one comparable
  PNG and required 24.12 seconds for preprocessing.

The experimental branch profiles are intentionally not dependencies: their
candidate filters were composed after the legacy v12 preprocessing chain,
whereas the lab score above measured the gamma recipe as a standalone transform.

Clean-room equivalence check on
`01aebc80de85-mixed-combined.png` (545 x 770) produced zero differing pixels
and the same complete PNG SHA-256 on Kornia CPU and the Stage 4 NumPy backend:
`d644ff7de664d1768d120cc258b25244c138875e7b5f1912a7ce077e8f8eddf9`.
