import assert from "node:assert/strict";
import test from "node:test";
import { MAX_BROWSER_PDF_BYTES, assertBrowserPdfSize } from "./pdf-limits";

test("assertBrowserPdfSize accepts files at the browser limit", () => {
  assert.doesNotThrow(() =>
    assertBrowserPdfSize({ size: MAX_BROWSER_PDF_BYTES }),
  );
});

test("assertBrowserPdfSize rejects oversized files", () => {
  assert.throws(
    () => assertBrowserPdfSize({ size: MAX_BROWSER_PDF_BYTES + 1 }),
    /browser limit/,
  );
});
