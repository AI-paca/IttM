import assert from "node:assert/strict";
import test from "node:test";
import {
  collectBrowserLayoutFeatures,
  type LayoutAnalysisRaster,
} from "./layout-features";

function whiteRaster(width: number, height: number): LayoutAnalysisRaster {
  return {
    data: new Uint8ClampedArray(width * height * 4).fill(255),
    width,
    height,
    sourceWidth: width * 4,
    sourceHeight: height * 4,
  };
}

function fillBlack(
  raster: LayoutAnalysisRaster,
  left: number,
  top: number,
  right: number,
  bottom: number,
) {
  for (let y = top; y < bottom; y += 1) {
    for (let x = left; x < right; x += 1) {
      const offset = (y * raster.width + x) * 4;
      raster.data[offset] = 0;
      raster.data[offset + 1] = 0;
      raster.data[offset + 2] = 0;
    }
  }
}

test("projection geometry collects local whitespace gutters and components", () => {
  const raster = whiteRaster(300, 240);
  for (let row = 0; row < 4; row += 1) {
    const top = row * 60 + 8;
    for (let column = 0; column < 3; column += 1) {
      const left = column * 100 + 10;
      fillBlack(raster, left, top, left + 70, top + 38);
    }
  }

  const features = collectBrowserLayoutFeatures(raster, [
    "projection_geometry",
  ]);
  const verticalWhitespace = features.separators.filter(
    (separator) => separator.axis === "x" && separator.kind === "whitespace",
  );

  assert.equal(features.width, 1200);
  assert.equal(features.height, 960);
  assert.ok(features.foregroundRatio > 0);
  assert.ok(features.components.length >= 12);
  assert.ok(
    verticalWhitespace.some(
      (separator) => separator.start < 400 && separator.end > 320,
    ),
  );
  assert.equal(features.scalars.analysisWidth, 300);
});

test("projection geometry reports real ink lines independently", () => {
  const raster = whiteRaster(120, 160);
  fillBlack(raster, 58, 0, 61, 160);

  const features = collectBrowserLayoutFeatures(raster, [
    "projection_geometry",
  ]);

  assert.ok(
    features.separators.some(
      (separator) =>
        separator.axis === "x" &&
        separator.kind === "ink" &&
        separator.start <= 240 &&
        separator.end >= 240,
    ),
  );
});

test("an empty image stays empty without selector-specific guesses", () => {
  const features = collectBrowserLayoutFeatures(whiteRaster(80, 60), [
    "projection_geometry",
  ]);

  assert.equal(features.foregroundRatio, 0);
  assert.deepEqual(features.components, []);
});
