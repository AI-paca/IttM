import assert from "node:assert/strict";
import test from "node:test";
import type { LayoutFeatures } from "./layout-contracts";
import { selectBrowserLayout } from "./layout-selectors";

const features: LayoutFeatures = {
  width: 1240,
  height: 27466,
  foregroundRatio: 0.12,
  separators: [],
  components: [],
  scalars: { aspectRatio: 27466 / 1240 },
};

test("uniform selector enables spatial stages without guessing document type", () => {
  const decision = selectBrowserLayout(features, {
    featureExtractors: ["projection_geometry"],
    selector: "uniform_spatial_v1",
    allowedStages: ["spatial_regions"],
    defaultParameters: { maxRegionHeight: 1400 },
  });

  assert.equal(decision.label, "spatial");
  assert.equal(decision.stages[0].name, "spatial_regions");
  assert.equal(decision.stages[0].parameters.minSourceWidth, 0);
  assert.equal(decision.stages[0].parameters.maxSourceWidth, "infinity");
});

test("selector output is limited by the profile allowlist", () => {
  const decision = selectBrowserLayout(features, {
    featureExtractors: ["projection_geometry"],
    selector: "uniform_spatial_v1",
    allowedStages: [],
    defaultParameters: {},
  });

  assert.equal(decision.label, "unsegmented");
  assert.deepEqual(decision.stages, []);
});
