import test from "node:test";
import assert from "node:assert/strict";
import {
  looksLikeDenseGridPixels,
  looksLikeSparseCoverPixels,
  overlappingStarts,
  planDenseGridCrops,
} from "./browser-dense-grid";

test("dense grid crop plan covers the page with overlapping OCR passes", () => {
  const crops = planDenseGridCrops(3300, 2200);

  assert.ok(crops.length > 100);
  assert.ok(crops.some((crop) => crop.pageSegmentationMode === "6"));
  assert.ok(crops.some((crop) => crop.pageSegmentationMode === "11"));
  assert.ok(crops.every((crop) => crop.sourceWidth > 0));
  assert.ok(crops.every((crop) => crop.targetWidth > 0));
});

test("overlapping starts include the trailing edge", () => {
  assert.deepEqual(overlappingStarts(100, 40, 10), [0, 30, 60]);
  assert.deepEqual(overlappingStarts(101, 40, 10), [0, 30, 60, 61]);
});

test("dense grid detector distinguishes long table rules from plain pixels", () => {
  const width = 800;
  const height = 500;
  const grid = new Uint8ClampedArray(width * height * 4).fill(255);
  const plain = new Uint8ClampedArray(grid);
  for (let y = 20; y < height; y += 45) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4;
      grid[offset] = grid[offset + 1] = grid[offset + 2] = 0;
    }
  }
  for (let x = 30; x < width; x += 70) {
    for (let y = 0; y < height; y += 1) {
      const offset = (y * width + x) * 4;
      grid[offset] = grid[offset + 1] = grid[offset + 2] = 0;
    }
  }

  assert.equal(looksLikeDenseGridPixels(grid, width, height), true);
  assert.equal(looksLikeDenseGridPixels(plain, width, height), false);
  assert.equal(looksLikeSparseCoverPixels(plain, width, height), true);
});
