import assert from "node:assert/strict";
import test from "node:test";
import { effectivePdfCropMode } from "./crop-preference";

test("manual crop preference falls back to automatic PDF cropping", () => {
  assert.equal(effectivePdfCropMode("auto"), "auto");
  assert.equal(effectivePdfCropMode("manual"), "auto");
  assert.equal(effectivePdfCropMode("none"), "none");
});
