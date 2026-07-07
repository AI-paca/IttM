import assert from "node:assert/strict";
import test from "node:test";
import { analyzeMarkdownGrammar } from "./markdown-diagnostics";

test("analyzeMarkdownGrammar reports tables, merge markers, and lint errors", () => {
  const diagnostics = analyzeMarkdownGrammar(
    [
      "# Result",
      "",
      "| A | B | C |",
      "| --- | --- | --- |",
      "| one | ::merge-left:: | three |",
      "| bad | row |",
      "",
      "- ",
    ].join("\n"),
  );

  assert.equal(diagnostics.status, "warn");
  assert.equal(diagnostics.tableCount, 1);
  assert.equal(diagnostics.tableRows, 4);
  assert.equal(diagnostics.maxColumns, 3);
  assert.equal(diagnostics.mergeMarkers, 1);
  assert.deepEqual(diagnostics.errors, [
    "line 6: table row has 2 cells, expected 3",
    "line 8: empty list item",
  ]);
});

test("analyzeMarkdownGrammar passes simple non-table markdown", () => {
  const diagnostics = analyzeMarkdownGrammar("Plain text\n\n- item");

  assert.equal(diagnostics.status, "pass");
  assert.equal(diagnostics.tableCount, 0);
  assert.deepEqual(diagnostics.errors, []);
});
