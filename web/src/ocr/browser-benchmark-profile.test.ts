import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_BROWSER_BENCHMARK_PROFILE,
  resolveBrowserBenchmarkProfile,
} from "../../../scripts/benchmark/browser-benchmark-profile";
import { browserPipelineProfileForSource } from "./pipeline-config";

test("browser benchmark defaults to the same profile as browser UI source", () => {
  const uiProfile = browserPipelineProfileForSource("browser");

  assert.equal(DEFAULT_BROWSER_BENCHMARK_PROFILE, uiProfile.name);
  assert.equal(
    resolveBrowserBenchmarkProfile().name,
    "browser_tesseract_dewarp",
  );
  assert.deepEqual(resolveBrowserBenchmarkProfile().imagePreprocessing, [
    "projector_slide_dewarp",
    "projected_document_dewarp",
    "browser_resize",
    "ocr_border",
  ]);
  assert.equal(resolveBrowserBenchmarkProfile().ocrBorderPixels, 10);
  assert.equal(resolveBrowserBenchmarkProfile().edgeWordFallbackPsm, "7");
  assert.equal(resolveBrowserBenchmarkProfile().lexicalCorrection, "t9_small");
  assert.equal(resolveBrowserBenchmarkProfile().ocrLanguageRetry, "t9_small");
  assert.equal(
    resolveBrowserBenchmarkProfile().tableSlotBuilder,
    "recursive_gaps_v1",
  );
  assert.equal(resolveBrowserBenchmarkProfile().tableSlotMaxColumns, 14);
});

test("browser benchmark accepts explicit diagnostic profiles", () => {
  assert.equal(
    resolveBrowserBenchmarkProfile("browser_tesseract_raw").name,
    "browser_tesseract_raw",
  );
  assert.equal(
    resolveBrowserBenchmarkProfile("browser_tesseract_greek_math").languages,
    "rus+eng+ell+equ",
  );
  assert.throws(
    () => resolveBrowserBenchmarkProfile("missing-profile"),
    /Unknown browser OCR profile 'missing-profile'/,
  );
});
