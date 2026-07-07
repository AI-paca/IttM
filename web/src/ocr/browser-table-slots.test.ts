import test from "node:test";
import assert from "node:assert/strict";
import {
  browserWordsToReviewedTableMarkdown,
  browserWordsToTableMarkdown,
} from "./browser-table-slots";
import type { BrowserOcrDetailedResult } from "./tesseract-worker-session";

function word(text: string, x0: number, y0: number, x1: number, y1: number) {
  return {
    text,
    confidence: 95,
    bbox: { x0, y0, x1, y1 },
  };
}

test("browser table slots build markdown from recursive word gaps", () => {
  const result: BrowserOcrDetailedResult = {
    text: "A B C\n1 2 3\n4 5 6",
    words: [
      word("A", 10, 10, 20, 24),
      word("B", 100, 10, 110, 24),
      word("C", 190, 10, 200, 24),
      word("1", 10, 40, 20, 54),
      word("2", 100, 40, 110, 54),
      word("3", 190, 40, 200, 54),
      word("4", 10, 70, 20, 84),
      word("5", 100, 70, 110, 84),
      word("6", 190, 70, 200, 84),
    ],
  };

  assert.equal(
    browserWordsToTableMarkdown(result),
    [
      "| A | B | C |",
      "| --- | --- | --- |",
      "| 1 | 2 | 3 |",
      "| 4 | 5 | 6 |",
    ].join("\n"),
  );
});

test("browser table reviewer clears noise without deleting its slot", async () => {
  const result: BrowserOcrDetailedResult = {
    text: "Name Value Note\nAlice 10 БЕ\nBob 20 ok",
    words: [
      word("Name", 10, 10, 45, 24),
      word("Value", 100, 10, 145, 24),
      word("Note", 190, 10, 225, 24),
      word("Alice", 10, 40, 55, 54),
      word("10", 100, 40, 120, 54),
      word("БЕ", 190, 40, 210, 54),
      word("Bob", 10, 70, 40, 84),
      word("20", 100, 70, 120, 84),
      word("ok", 190, 70, 210, 84),
    ],
  };

  const markdown = await browserWordsToReviewedTableMarkdown(
    result,
    {},
    async ({ text }) => text !== "БЕ",
  );

  assert.match(markdown, /^\| Name \| Value \| Note \|/m);
  assert.match(markdown, /^\| Alice \| 10 \|  \|$/m);
  assert.match(markdown, /^\| Bob \| 20 \| ok \|$/m);
});

test("browser table slots leave ordinary prose alone", () => {
  const result: BrowserOcrDetailedResult = {
    text: "one ordinary paragraph without table geometry",
    words: [
      word("one", 10, 10, 35, 24),
      word("ordinary", 45, 10, 110, 24),
      word("paragraph", 120, 10, 190, 24),
      word("without", 200, 10, 250, 24),
      word("table", 260, 10, 300, 24),
      word("geometry", 310, 10, 380, 24),
    ],
  };

  assert.equal(browserWordsToTableMarkdown(result), "");
});

test("browser table slots allow wide tables only when profile raises the column cap", () => {
  const words = Array.from({ length: 10 }, (_, rowIndex) =>
    Array.from({ length: 10 }, (_, col) =>
      word(
        rowIndex === 0 ? `H${col + 1}` : `R${rowIndex}C${col + 1}`,
        10 + col * 80,
        10 + rowIndex * 30,
        35 + col * 80,
        24 + rowIndex * 30,
      ),
    ),
  ).flat();
  const result: BrowserOcrDetailedResult = {
    text: "wide 10 column table",
    words,
  };

  assert.equal(browserWordsToTableMarkdown(result), "");
  assert.match(
    browserWordsToTableMarkdown(result, { maxColumns: 14 }),
    /^\| H1 \| H2 \| H3 \| H4 \| H5 \| H6 \| H7 \| H8 \| H9 \| H10 \|/m,
  );
});

test("browser table slots reject narrow pseudo-tables even with a wide cap", () => {
  const result: BrowserOcrDetailedResult = {
    text: "Contribution activity June 2026",
    words: [
      word("June", 10, 10, 50, 24),
      word("2026", 100, 10, 140, 24),
      word("Created", 10, 40, 60, 54),
      word("94", 100, 40, 120, 54),
      word("commits", 220, 40, 270, 54),
      word("repository", 360, 40, 430, 54),
      word("AI-paca/IttM", 100, 70, 190, 84),
      word("merged", 300, 70, 350, 84),
      word("Jun", 500, 100, 530, 114),
      word("26", 580, 100, 600, 114),
    ],
  };

  assert.equal(browserWordsToTableMarkdown(result, { maxColumns: 14 }), "");
});

test("browser table slots reject sparse overwide UI noise", () => {
  const result: BrowserOcrDetailedResult = {
    text: "Contribution activity June 2026",
    words: [
      word("Contribution", 100, 10, 190, 24),
      word("activity", 200, 10, 260, 24),
      word("June", 10, 40, 50, 54),
      word("2026", 100, 40, 140, 54),
      word("Created", 10, 70, 60, 84),
      word("94", 100, 70, 120, 84),
      word("commits", 220, 70, 270, 84),
      word("repository", 360, 70, 430, 84),
      word("Jun", 500, 100, 530, 114),
      word("26", 580, 100, 600, 114),
    ],
  };

  assert.equal(browserWordsToTableMarkdown(result), "");
});
