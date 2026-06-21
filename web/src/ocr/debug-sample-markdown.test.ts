import test from "node:test";
import assert from "node:assert/strict";
import { debugMarkdownForFileName } from "./debug-sample-markdown";

test("debug markdown fixture is dev-gated and limited to the sample table", () => {
  assert.equal(
    debugMarkdownForFileName("SAMPLE_mixed_ru_en_zh_table_image.pdf", false),
    null,
  );
  assert.match(
    debugMarkdownForFileName("SAMPLE_mixed_ru_en_zh_table_image.pdf", true) ||
      "",
    /\| РАЗДЕЛ A .* \| ::merge-left:: \| ::merge-left:: \|/,
  );
  assert.equal(debugMarkdownForFileName("photo.jpg", true), null);
});
