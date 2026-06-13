import assert from "node:assert/strict";
import test from "node:test";
import { findContentBounds } from "./image-content-bounds";

function whiteImage(width: number, height: number): Uint8ClampedArray {
  return new Uint8ClampedArray(width * height * 4).fill(255);
}

test("findContentBounds returns padded non-white pixel bounds", () => {
  const width = 8;
  const height = 6;
  const data = whiteImage(width, height);
  const offset = (3 * width + 4) * 4;
  data[offset] = 0;
  data[offset + 1] = 0;
  data[offset + 2] = 0;

  assert.deepEqual(findContentBounds(data, width, height, 240, 1), {
    left: 3,
    top: 2,
    right: 6,
    bottom: 5,
  });
});

test("findContentBounds keeps an all-white image intact", () => {
  assert.deepEqual(findContentBounds(whiteImage(4, 3), 4, 3), {
    left: 0,
    top: 0,
    right: 4,
    bottom: 3,
  });
});
