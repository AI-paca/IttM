import test from "node:test";
import assert from "node:assert/strict";
import {
  BROWSER_PIPELINE_PROFILES,
  browserPipelineProfileForSource,
} from "./pipeline-config";

test("browser source enables projected document dewarp before OCR", () => {
  const profile = browserPipelineProfileForSource("browser");

  assert.deepEqual(profile.imagePreprocessing, [
    "projector_slide_dewarp",
    "projected_document_dewarp",
    "browser_resize",
    "ocr_border",
  ]);
  assert.equal(profile.ocrBorderPixels, 10);
  assert.equal(profile.denseGridFallback, true);
  assert.equal(profile.denseGridTargetWidth, 3300);
  assert.equal(profile.edgeWordFallbackPsm, "7");
  assert.deepEqual(profile.layout.featureExtractors, ["projection_geometry"]);
  assert.equal(profile.layout.selector, "uniform_spatial_v1");
  assert.deepEqual(profile.layout.allowedStages, ["spatial_regions"]);
});

test("projected document dewarp remains isolated from the standard profile", () => {
  assert.deepEqual(
    BROWSER_PIPELINE_PROFILES.browser_tesseract_standard.imagePreprocessing,
    ["browser_resize", "ocr_border"],
  );
  assert.deepEqual(
    BROWSER_PIPELINE_PROFILES.browser_tesseract_dewarp.imagePreprocessing,
    [
      "projector_slide_dewarp",
      "projected_document_dewarp",
      "browser_resize",
      "ocr_border",
    ],
  );
});
