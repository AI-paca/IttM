import assert from "node:assert/strict";
import test from "node:test";
import { boundedViewportScale, processPreparedPages } from "./pdf-processing";

test("boundedViewportScale enforces dimension and pixel limits", () => {
  assert.equal(
    boundedViewportScale(8000, 4000, {
      maxDimension: 4096,
      maxPagePixels: 12_000_000,
    }),
    4096 / 8000,
  );
  assert.equal(
    boundedViewportScale(8000, 4000, {
      maxDimension: 10_000,
      maxPagePixels: 12_000_000,
    }),
    Math.sqrt(12_000_000 / 32_000_000),
  );
  assert.equal(
    boundedViewportScale(2000, 1000, {
      maxDimension: 4096,
      maxPagePixels: 12_000_000,
    }),
    1,
  );
});

test("processPreparedPages handles one page at a time in order", async () => {
  const events: string[] = [];
  const chunks: number[] = [];

  const result = await processPreparedPages(
    3,
    1,
    {},
    () => {},
    async (pageNumber) => {
      events.push(`prepare:${pageNumber}`);
      return {
        nativeText: "",
        image: new Blob([String(pageNumber)]),
      };
    },
    async (image) => {
      const pageNumber = await image.text();
      events.push(`ocr:${pageNumber}`);
      return `page ${pageNumber}`;
    },
    (_text, pageNumber) => chunks.push(pageNumber ?? 0),
  );

  assert.deepEqual(events, [
    "prepare:1",
    "ocr:1",
    "prepare:2",
    "ocr:2",
    "prepare:3",
    "ocr:3",
  ]);
  assert.deepEqual(chunks, [1, 2, 3]);
  assert.equal(result, "page 1\n\n---\n\npage 2\n\n---\n\npage 3");
});

test("processPreparedPages stops before starting the next cancelled page", async () => {
  let active = true;
  let preparedPages = 0;

  await assert.rejects(
    processPreparedPages(
      2,
      1,
      { shouldContinue: () => active },
      () => {},
      async () => {
        preparedPages += 1;
        return { nativeText: "", image: new Blob(["page"]) };
      },
      async () => {
        active = false;
        return "page";
      },
    ),
    (error: unknown) =>
      error instanceof DOMException && error.name === "AbortError",
  );
  assert.equal(preparedPages, 1);
});
