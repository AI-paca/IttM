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
  assert.match(markdown, /^\| Alice \| 10 \| {2}\|$/m);
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

test("browser table slots reject long prose wrapped as a narrow grid", () => {
  const words = Array.from({ length: 45 }, (_, rowIndex) => {
    const columns = rowIndex % 3 === 0 ? [0, 1, 2] : [0, 1];
    if (rowIndex === 0) columns.pop();
    return columns.map((col) =>
      word(
        `text${rowIndex}-${col}`,
        10 + col * 170,
        10 + rowIndex * 24,
        85 + col * 170,
        24 + rowIndex * 24,
      ),
    );
  }).flat();
  const result: BrowserOcrDetailedResult = {
    text: "long prose document",
    words,
  };

  assert.equal(browserWordsToTableMarkdown(result, { maxColumns: 4 }), "");
});

test("browser table slots reject long sparse wide prose grids", () => {
  const words = Array.from({ length: 48 }, (_, rowIndex) => {
    const columns =
      rowIndex < 10 ? Array.from({ length: 9 }, (_, col) => col) : [0, 4, 8];
    return columns.map((col) =>
      word(
        `text${rowIndex}-${col}`,
        10 + col * 105,
        10 + rowIndex * 24,
        45 + col * 105,
        24 + rowIndex * 24,
      ),
    );
  }).flat();
  const result: BrowserOcrDetailedResult = {
    text: "long sparse prose document",
    words,
  };

  assert.equal(browserWordsToTableMarkdown(result, { maxColumns: 14 }), "");
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

test("browser table slots recover coarse columns from oversegmented word gaps", () => {
  const tableWords = Array.from({ length: 10 }, (_, rowIndex) =>
    Array.from({ length: 6 }, (_, col) => {
      const left = 10 + col * 150;
      const top = 100 + rowIndex * 30;
      const prefix = rowIndex === 0 ? `H${col + 1}` : `R${rowIndex}C${col + 1}`;
      return [
        word(prefix, left, top, left + 35, top + 14),
        word("part", left + 55, top, left + 90, top + 14),
      ];
    }),
  ).flat(2);
  const result: BrowserOcrDetailedResult = {
    text: "six column dense table",
    words: [
      word("nav", 10, 10, 35, 24),
      word("search", 380, 10, 430, 24),
      word("promo", 760, 40, 810, 54),
      ...tableWords,
      word("footer", 10, 430, 60, 444),
    ],
  };

  const markdown = browserWordsToTableMarkdown(result, { maxColumns: 6 });

  assert.match(
    markdown,
    /^\| H1 part \| H2 part \| H3 part \| H4 part \| H5 part \| H6 part \|/m,
  );
  assert.match(
    markdown,
    /^\| R1C1 part \| R1C2 part \| R1C3 part \| R1C4 part \| R1C5 part \| R1C6 part \|/m,
  );
  assert.doesNotMatch(markdown, /nav|promo|footer/);
});

test("browser table slots normalize repeated product-card grids", async () => {
  const rows = [
    [
      "Price",
      "HP laptop",
      "ASUS Vivobook",
      "Lenovo IdeaPad",
      "HP Laptop",
      "Laptop PC",
    ],
    [
      "Brands",
      "FHD Display",
      "Windows Notebook",
      "IPS 512GB",
      "FHD 16GB",
      "Core Celeron",
    ],
    ["", "Windows 11", "Fast Processor", "Office SSD", "Windows 11", "USB C"],
    ["", "4.1 (12)", "4.2 (2)", "3.9 (10)", "4.4 (20)", "4.0 (1)"],
    ["", "Bought 100", "Bought 50", "Bought 10", "Bought 200", "Bought 20"],
    [
      "",
      "Prime Day Deal",
      "Limited deal",
      "Prime exclusive",
      "Deal",
      "Prime Day Deal",
    ],
    ["", "€259.99", "£474.00", "£499.00", "£379.00", "£189.00"],
    ["", "Exclusive Prime price", "", "60% off Microsoft", "rrp £500", ""],
    [
      "",
      "Add to basket",
      "See options",
      "Add to basket",
      "See options",
      "Add to basket",
    ],
  ];
  const words = rows.flatMap((row, rowIndex) =>
    row.flatMap((cell, col) => {
      if (!cell) return [];
      const left = 10 + col * 190;
      const top = 100 + rowIndex * 30;
      const parts = cell.split(/\s+/);
      return parts.map((part, partIndex) =>
        word(
          part,
          left + partIndex * 55,
          top,
          left + partIndex * 55 + 35,
          top + 14,
        ),
      );
    }),
  );
  const result: BrowserOcrDetailedResult = {
    text: "product card grid",
    words,
  };

  const markdown = browserWordsToTableMarkdown(result, { maxColumns: 6 });

  assert.match(
    markdown,
    /^\| Field \| Result 1 \| Result 2 \| Result 3 \| Result 4 \| Result 5 \|/m,
  );
  assert.match(markdown, /^\| Product \| HP laptop FHD Display Windows 11/m);
  assert.match(markdown, /^\| Price \| €259\.99 \| £474\.00/m);
  assert.match(markdown, /^\| Action \| Add to basket \| See options/m);
  assert.match(markdown, /^Price$/m);
  assert.match(markdown, /^Brands$/m);

  const reviewed = await browserWordsToReviewedTableMarkdown(
    result,
    { maxColumns: 6 },
    async () => true,
  );
  assert.match(reviewed, /^Price$/m);
  assert.match(reviewed, /^Brands$/m);
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
