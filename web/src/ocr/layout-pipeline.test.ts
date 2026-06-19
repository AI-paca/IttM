import assert from "node:assert/strict";
import test from "node:test";
import type { LayoutAnalysisRaster } from "./layout-features";
import { planBrowserLayoutRegions } from "./layout-pipeline";

function threeColumnRaster(): LayoutAnalysisRaster {
  const width = 300;
  const height = 180;
  const data = new Uint8ClampedArray(width * height * 4).fill(255);
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 3; column += 1) {
      const top = row * 60 + 8;
      const left = column * 100 + 10;
      for (let y = top; y < top + 38; y += 1) {
        for (let x = left; x < left + 70; x += 1) {
          const offset = (y * width + x) * 4;
          data[offset] = 0;
          data[offset + 1] = 0;
          data[offset + 2] = 0;
        }
      }
    }
  }
  return {
    data,
    width,
    height,
    sourceWidth: 1200,
    sourceHeight: 720,
  };
}

test("browser layout pipeline keeps extraction, selection and execution compatible", () => {
  const result = planBrowserLayoutRegions(threeColumnRaster(), {
    featureExtractors: ["projection_geometry"],
    selector: "uniform_spatial_v1",
    allowedStages: ["spatial_regions"],
    defaultParameters: {
      maxRegionHeight: 720,
      minRegionHeight: 200,
      minRegionWidth: 80,
      minSeparatorCoverage: 0.55,
    },
  });

  assert.equal(result.decision.label, "spatial");
  assert.equal(result.regions.length, 3);
  assert.equal(
    result.regions.reduce((total, region) => total + region.sourceWidth, 0),
    1200,
  );
  assert.ok(
    result.regions.every(
      (region) => region.sourceWidth >= 360 && region.sourceWidth <= 440,
    ),
  );
});
