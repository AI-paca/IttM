import test from "node:test";
import assert from "node:assert/strict";
import { planImageTiles, planRegionTiles } from "./image-tiling";

test("long screenshot tiles preserve readable width and cover the full height", () => {
  const width = 1240;
  const height = 27466;
  const tiles = planImageTiles(width, height, {
    maxImagePixels: 14_000_000,
    maxDimension: 4200,
  });

  assert.ok(tiles.length > 1);
  assert.equal(tiles[0].sourceY, 0);
  assert.equal(tiles.at(-1)!.sourceY + tiles.at(-1)!.sourceHeight, height);
  assert.ok(tiles.every((tile) => tile.targetWidth === width));
  assert.ok(
    tiles.every(
      (tile) =>
        tile.targetWidth * tile.targetHeight <= 14_000_000 &&
        tile.targetHeight <= 4200,
    ),
  );

  for (let index = 1; index < tiles.length; index += 1) {
    const previous = tiles[index - 1];
    const current = tiles[index];
    assert.ok(
      current.sourceY < previous.sourceY + previous.sourceHeight,
      "adjacent OCR tiles must overlap",
    );
    assert.ok(
      current.sourceY <= previous.sourceY + previous.sourceHeight,
      "adjacent OCR tiles must not leave gaps",
    );
  }
});

test("ordinary images stay a single bounded OCR input", () => {
  const tiles = planImageTiles(8000, 8000, {
    maxImagePixels: 14_000_000,
    maxDimension: 4200,
  });

  assert.equal(tiles.length, 1);
  assert.ok(tiles[0].targetWidth <= 4200);
  assert.ok(tiles[0].targetHeight <= 4200);
  assert.ok(tiles[0].targetWidth * tiles[0].targetHeight <= 14_000_000);
});

test("extreme one-pixel-wide screenshots terminate with bounded tiles", () => {
  const tiles = planImageTiles(1, 50_000, {
    maxImagePixels: 4_000_000,
    maxDimension: 2200,
  });

  assert.ok(tiles.length > 1);
  assert.equal(tiles.at(-1)!.sourceY + tiles.at(-1)!.sourceHeight, 50_000);
  assert.ok(tiles.every((tile) => tile.targetHeight <= 2200));
});

test("invalid image geometry is rejected before canvas allocation", () => {
  assert.throws(
    () =>
      planImageTiles(-1920, 1080, {
        maxImagePixels: 4_000_000,
        maxDimension: 2200,
      }),
    /must be positive/,
  );
});

test("layout regions are bounded independently without losing coordinates", () => {
  const tiles = planRegionTiles(
    [
      {
        sourceX: 0,
        sourceY: 0,
        sourceWidth: 3000,
        sourceHeight: 1000,
      },
      {
        sourceX: 3000,
        sourceY: 0,
        sourceWidth: 3000,
        sourceHeight: 1000,
      },
    ],
    {
      maxImagePixels: 2_000_000,
      maxDimension: 2200,
    },
  );

  assert.equal(tiles.length, 2);
  assert.equal(tiles[1].sourceX, 3000);
  assert.ok(
    tiles.every(
      (tile) =>
        tile.targetWidth * tile.targetHeight <= 2_000_000 &&
        tile.targetWidth <= 2200 &&
        tile.targetHeight <= 2200,
    ),
  );
});
