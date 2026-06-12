# Tesseract Quality Experiments

Branch: `engine-tesseract-quality`, based on local PR-ready `engine`.

## Rules

- Browser Tesseract.js, backend Tesseract and EasyOCR remain independent
  implementations.
- A stage must be configurable per profile; no Python filter is copied into the
  browser runtime.
- Raw OCR text is the fallback when structural confidence is insufficient.
- Every accepted change must preserve names, digits and reading order on the
  image matrix and keep real curriculum tables detected.

## Baseline

Commit `f339fd9`:

| Fixture | Backend Tesseract | EasyOCR CPU | Browser Tesseract.js |
| --- | --- | --- | --- |
| `image (6).png` | 1.04 s, false 570-cell table, rankings lost | 3.08 s, false table, partial names | 2.52 s, useful raw text with ranks 1-10 and all scores |
| `photo_10...jpg` | 0.72 s, readable slide | 2.93 s, loses several lines | Node harness returned empty text; full DOM/Canvas path is required |
| Long catalog screenshot | 8.61 s, noisy text | 43.82 s, more text but far above target | 8.75 s, noisy text |

The first browser attempt failed because local language files were absent. The
runner recorded exit `1`; the repeat used `chi_sim`, `eng` and `rus` data copied
from the local OCR Docker image.

## Experiment 1: Profile Grid Confidence

Commit `50ab3fa` adds `grid_min_confirmed_cell_ratio` to the Python pipeline
profile. The morphology detector accepts a table only when enough enclosed
rectangular cells are confirmed by contours. The standard backend profiles use
`0.35`; profiles without table analysis are unaffected.

Result on `image (6).png`:

| Engine | Before | After | Visual result |
| --- | ---: | ---: | --- |
| Tesseract | 1.04 s | 1.45 s | Keeps title, ranks 1-10, names and all ten scores |
| EasyOCR CPU | 3.08 s | 4.81 s | Keeps most names and scores as raw text; some field order remains wrong |

The change removes destructive table reconstruction rather than pretending the
bar chart is a conventional grid. Diagram-to-table reconstruction remains a
valid future feature, but it must consume the retained raw rows in a separate,
profile-controlled stage.

## Regression Checks

- The slide and long catalog OCR bodies are byte-identical before and after the
  grid-confidence change for both backend engines.
- `000041301_UchebPlan_sign000029629.pdf` still reports 4 tables and 17,780
  cells.
- Full backend suite: 37 passed, 7 skipped.
- Black and Ruff passed.

The curriculum PDF took 108.5 s with Tesseract and 62.0 s with EasyOCR. This is
quality evidence only; performance work belongs to `engine-time-optimization`.

## Next Quality Step

Build an optional row-reconstruction stage from the retained raw OCR lines:

1. Detect a complete rank sequence and score column without deleting source
   text.
2. Emit Markdown only when coverage is high; otherwise return raw text.
3. Configure the stage independently for backend Tesseract, EasyOCR and browser
   Tesseract.js.
4. Compare every engine against the committed baseline before accepting it.
