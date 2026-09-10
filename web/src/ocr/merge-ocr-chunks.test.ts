import test from "node:test";
import assert from "node:assert/strict";
import { mergeOcrTextChunks, selectBetterOcrChunk } from "./merge-ocr-chunks";

const sharedCore = {
  shouldDropTextBlock(options: {
    candidateChars: number;
    existingChars: number;
    sharedTokens: number;
    candidateTokens: number;
    existingTokens: number;
    similarityMilli: number;
  }): boolean {
    if (options.candidateChars < 32 || options.existingChars < 32) return false;
    return (
      options.similarityMilli >= 880 ||
      options.sharedTokens * 1_000 >= options.candidateTokens * 850
    );
  },
};

test("overlapping long-screenshot OCR chunks keep every row once", () => {
  const merged = mergeOcrTextChunks([
    "PRODUCT-000 1000.99\nPRODUCT-001 1001.99\nPRODUCT-002 1002.99",
    "PRODUCT-002 1002.99\nPRODUCT-003 1003.99\nPRODUCT-004 1004.99",
    "PRODUCT-004 1004.99\nPRODUCT-005 1005.99",
  ]);

  assert.equal(
    merged,
    [
      "PRODUCT-000 1000.99",
      "PRODUCT-001 1001.99",
      "PRODUCT-002 1002.99",
      "PRODUCT-003 1003.99",
      "PRODUCT-004 1004.99",
      "PRODUCT-005 1005.99",
    ].join("\n"),
  );
});

test("similar but different product rows are never deduplicated", () => {
  const merged = mergeOcrTextChunks(
    [
      "PRODUCT-010 consistent long description 1010.99 available",
      "PRODUCT-011 consistent long description 1011.99 available",
    ],
    sharedCore,
  );

  assert.match(merged, /PRODUCT-010/);
  assert.match(merged, /PRODUCT-011/);
});

test("shared core removes repeated multi-line OCR blocks before grammar", () => {
  const products = [
    "PRODUCT-010 consistent long description 1010.99 available",
    "PRODUCT-011 consistent long description 1011.99 available",
  ].join("\n");
  const merged = mergeOcrTextChunks(
    [
      `Store heading and controls for the captured page\n\n${products}`,
      `${products.toLocaleLowerCase()}\n\nFooter text that is unique to the segmented pass`,
    ],
    sharedCore,
  );

  assert.equal(merged.match(/PRODUCT-010/gi)?.length, 1);
  assert.equal(merged.match(/PRODUCT-011/gi)?.length, 1);
  assert.match(merged, /Footer text that is unique/);
});

test("boundary comparison tolerates whitespace and OCR casing only", () => {
  assert.equal(
    mergeOcrTextChunks(["Header\n  Product A  ", "product a\nFooter"]),
    "Header\n  Product A  \nFooter",
  );
});

test("alternative browser OCR chunks prefer structured table output", () => {
  const prose = "A B C\n1 2 3\n4 5 6";
  const table = [
    "| A | B | C |",
    "| --- | --- | --- |",
    "| 1 | 2 | 3 |",
    "| 4 | 5 | 6 |",
  ].join("\n");

  assert.equal(selectBetterOcrChunk(prose, table), table);
});

test("alternative browser OCR chunks reject sparse pseudo-tables", () => {
  const prose = Array.from(
    { length: 48 },
    (_, index) => `text${index}-0 text${index}-2 text${index}-5`,
  ).join("\n");
  const table = [
    "| Column 1 | Column 2 | Column 3 | Column 4 | Column 5 | Column 6 | Column 7 | Column 8 | Column 9 |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ...Array.from({ length: 48 }, (_, index) =>
      index % 5 === 0
        ? `| text${index}-0 | text${index}-1 | text${index}-2 | text${index}-3 |  |  |  |  |  |`
        : `| text${index}-0 |  | text${index}-2 |  |  | text${index}-5 |  |  |  |`,
    ),
  ].join("\n");

  assert.equal(selectBetterOcrChunk(prose, table), prose);
});

test("alternative browser OCR chunks keep the incumbent on near ties", () => {
  assert.equal(
    selectBetterOcrChunk("Product 1 price 10", "Product I price l0"),
    "Product 1 price 10",
  );
});
