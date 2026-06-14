import test from "node:test";
import assert from "node:assert/strict";
import { mergeOcrTextChunks } from "./merge-ocr-chunks";

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
  const merged = mergeOcrTextChunks([
    "PRODUCT-010 1010.99",
    "PRODUCT-011 1011.99",
  ]);

  assert.match(merged, /PRODUCT-010/);
  assert.match(merged, /PRODUCT-011/);
});

test("boundary comparison tolerates whitespace and OCR casing only", () => {
  assert.equal(
    mergeOcrTextChunks(["Header\n  Product A  ", "product a\nFooter"]),
    "Header\n  Product A  \nFooter",
  );
});
