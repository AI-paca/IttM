import test from "node:test";
import assert from "node:assert/strict";
import {
  BROWSER_PIPELINE_PROFILES,
  backendPipelineParams,
  browserPipelineProfileForSource,
  normalizeBrowserPipelineProfile,
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
  assert.equal(profile.spatialFullPageFallback, true);
  assert.equal(profile.darkUiTextFallback, true);
  assert.equal(profile.contextualMarkdownGrammar, true);
  assert.equal(profile.denseGridTargetWidth, 3300);
  assert.equal(profile.edgeWordFallbackPsm, "7");
  assert.equal(profile.lexicalCorrection, "t9_small");
  assert.equal(profile.ocrLanguageRetry, "t9_small");
  assert.equal(profile.tableSlotBuilder, "recursive_gaps_v1");
  assert.equal(profile.tableSlotMaxColumns, 14);
  assert.equal(profile.recursiveTableCellOcr, "auto");
  assert.equal(profile.recursiveTableCellOcrBatchPixels, 8_000_000);
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

test("table-first browser profile is explicit opt-in", () => {
  const profile = BROWSER_PIPELINE_PROFILES.browser_tesseract_table_first;

  assert.equal(profile.layout.selector, "table_first_heuristic_v1");
  assert.deepEqual(profile.layout.allowedStages, [
    "table_regions",
    "spatial_regions",
  ]);
});

test("table-slot browser profile is explicit opt-in", () => {
  const profile = BROWSER_PIPELINE_PROFILES.browser_tesseract_table_slots;

  assert.equal(profile.tableSlotBuilder, "recursive_gaps_v1");
  assert.equal(profile.lexicalCorrection, "off");
  assert.equal(profile.ocrLanguageRetry, "off");
  assert.equal(profile.tableSlotMaxColumns, 14);
  assert.equal(profile.recursiveTableCellOcr, "auto");
  assert.equal(profile.recursiveTableCellOcrBatchPixels, 8_000_000);
  assert.equal(profile.layout.selector, "table_first_heuristic_v1");
  assert.deepEqual(profile.layout.allowedStages, [
    "table_regions",
    "spatial_regions",
  ]);
});

test("table-slot small-reviewer browser profile is explicit opt-in", () => {
  const profile = BROWSER_PIPELINE_PROFILES.browser_tesseract_table_slots_t9;

  assert.equal(profile.tableSlotBuilder, "recursive_gaps_v1");
  assert.equal(profile.lexicalCorrection, "t9_small");
  assert.equal(profile.ocrLanguageRetry, "t9_small");
  assert.equal(profile.tableSlotMaxColumns, 14);
  assert.equal(profile.recursiveTableCellOcr, "auto");
  assert.equal(profile.recursiveTableCellOcrBatchPixels, 8_000_000);
  assert.equal(profile.layout.selector, "table_first_heuristic_v1");
});

test("tiny reviewer mode enables matching ocr language retry without table slots", () => {
  const profile = normalizeBrowserPipelineProfile({
    ...BROWSER_PIPELINE_PROFILES.browser_tesseract_table_first,
    name: "normalized-table-slot-test",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "off",
    tableSlotBuilder: "off",
  });

  assert.equal(profile.lexicalCorrection, "t9_small");
  assert.equal(profile.ocrLanguageRetry, "t9_small");
  assert.equal(profile.tableSlotBuilder, "off");
});

test("greek math browser profile is explicit opt-in", () => {
  const profile = BROWSER_PIPELINE_PROFILES.browser_tesseract_greek_math;

  assert.equal(profile.languages, "rus+eng+ell+equ");
  assert.equal(profile.lexicalCorrection, "t9_small");
  assert.equal(profile.ocrLanguageRetry, "t9_small");
  assert.equal(profile.tableSlotBuilder, "recursive_gaps_v1");
  assert.equal(profile.tableSlotMaxColumns, 14);
  assert.equal(profile.layout.selector, "uniform_spatial_v1");
  assert.deepEqual(profile.layout.allowedStages, ["spatial_regions"]);
});

test("backend params include lexical correction flag only when requested", () => {
  assert.deepEqual(backendPipelineParams("local_tess"), {
    pipeline_profile: "backend_tesseract_standard",
  });
  assert.deepEqual(backendPipelineParams("local_tess", true), {
    pipeline_profile: "backend_tesseract_standard",
    pipeline_flags: "lexical_correction:t9_small;ocr_language_retry:t9_small",
  });
});
