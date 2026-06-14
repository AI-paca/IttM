import test from "node:test";
import assert from "node:assert/strict";
import {
  BROWSER_PIPELINE_PROFILES,
  browserPipelineProfileForSource,
} from "./pipeline-config";

test("standard browser OCR keeps preprocessing in the resize worker", () => {
  const profile = browserPipelineProfileForSource("browser");

  assert.deepEqual(profile.imagePreprocessing, ["browser_resize"]);
  assert.equal(
    profile.imagePreprocessing.includes("projected_document_dewarp"),
    false,
  );
  assert.deepEqual(profile.layout.featureExtractors, ["projection_geometry"]);
  assert.equal(profile.layout.selector, "uniform_spatial_v1");
  assert.deepEqual(profile.layout.allowedStages, ["spatial_regions"]);
});

test("projected document dewarp remains isolated from the standard profile", () => {
  assert.deepEqual(
    BROWSER_PIPELINE_PROFILES.browser_tesseract_dewarp.imagePreprocessing,
    ["projected_document_dewarp", "browser_resize"],
  );
});
