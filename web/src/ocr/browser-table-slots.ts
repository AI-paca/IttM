import type {
  BrowserOcrDetailedResult,
  BrowserOcrWordBox,
} from "./tesseract-worker-session";
import type { BrowserTextCandidate } from "./text-reviewer-protocol";

const MERGE_LEFT = "::merge-left::";

interface BrowserTableSlotOptions {
  maxColumns?: number;
}

interface SlotWord {
  text: string;
  confidence: number | null;
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function cleanWord(text: string): string {
  return text
    .trim()
    .replace(/[|[\]'"\u2018\u2019\u201c\u201d]+/g, "")
    .replace(/\s+/g, " ");
}

function toSlotWord(word: BrowserOcrWordBox): SlotWord | null {
  const text = cleanWord(word.text);
  if (!text) return null;
  const { x0, y0, x1, y1 } = word.bbox;
  if (x1 <= x0 || y1 <= y0) return null;
  return {
    text: text.replaceAll("|", "\\|"),
    confidence: word.confidence,
    left: x0,
    top: y0,
    right: x1,
    bottom: y1,
  };
}

function median(values: readonly number[], fallback: number): number {
  if (values.length === 0) return fallback;
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  if (ordered.length % 2 === 1) return ordered[middle];
  return (ordered[middle - 1] + ordered[middle]) / 2;
}

function groupRows(words: readonly SlotWord[]): SlotWord[][] {
  const heights = words.map((word) => word.bottom - word.top);
  const tolerance = Math.max(6, median(heights, 12) * 0.72);
  const rows: Array<{ center: number; words: SlotWord[] }> = [];

  for (const word of [...words].sort(
    (a, b) =>
      (a.top + a.bottom) / 2 - (b.top + b.bottom) / 2 || a.left - b.left,
  )) {
    const center = (word.top + word.bottom) / 2;
    const current = rows[rows.length - 1];
    if (!current || Math.abs(center - current.center) > tolerance) {
      rows.push({ center, words: [word] });
      continue;
    }
    current.words.push(word);
    current.center =
      (current.center * (current.words.length - 1) + center) /
      current.words.length;
  }

  return rows.map((row) => row.words.sort((a, b) => a.left - b.left));
}

function recursiveCuts(words: readonly SlotWord[], minGap: number): number[] {
  const ordered = [...words].sort(
    (a, b) => a.left - b.left || a.right - b.right,
  );
  if (ordered.length < 2) return [];

  let bestGap = 0;
  let cut = 0;
  for (let index = 1; index < ordered.length; index += 1) {
    const gap = ordered[index].left - ordered[index - 1].right;
    if (gap >= minGap && gap > bestGap) {
      bestGap = gap;
      cut = ordered[index].left;
    }
  }
  if (bestGap <= 0) return [];

  const left = ordered.filter((word) => word.right <= cut);
  const right = ordered.filter((word) => word.left >= cut);
  return [...recursiveCuts(left, minGap), cut, ...recursiveCuts(right, minGap)];
}

function mergeLines(lines: readonly number[], tolerance: number): number[] {
  const ordered = [...new Set(lines.map((line) => Math.round(line)))].sort(
    (a, b) => a - b,
  );
  if (ordered.length === 0) return [];
  const groups: number[][] = [[ordered[0]]];
  for (const line of ordered.slice(1)) {
    const group = groups[groups.length - 1];
    if (line - group[group.length - 1] <= tolerance) {
      group.push(line);
    } else {
      groups.push([line]);
    }
  }
  return groups.map((group) =>
    Math.round(group.reduce((sum, line) => sum + line, 0) / group.length),
  );
}

function positionToInterval(
  position: number,
  lines: readonly number[],
): number | null {
  if (
    lines.length < 2 ||
    position < lines[0] ||
    position > lines[lines.length - 1]
  )
    return null;
  if (position === lines[lines.length - 1]) return lines.length - 2;
  for (let index = 0; index < lines.length - 1; index += 1) {
    if (lines[index] <= position && position < lines[index + 1]) return index;
  }
  return null;
}

function tableCellsForLines(
  rows: readonly SlotWord[][],
  xLines: readonly number[],
): string[][] {
  return rows.map((rowWords) => {
    const cells = Array<string>(xLines.length - 1).fill("");
    const mergeCells = new Set<number>();
    for (const word of rowWords) {
      const center = (word.left + word.right) / 2;
      const col = positionToInterval(center, xLines);
      if (col === null) continue;
      cells[col] = [cells[col], word.text].filter(Boolean).join(" ").trim();
      for (
        let boundary = col + 1;
        boundary < xLines.length - 1;
        boundary += 1
      ) {
        if (
          word.left + 1 < xLines[boundary] &&
          xLines[boundary] < word.right - 1
        ) {
          mergeCells.add(boundary);
        }
      }
    }
    for (const col of mergeCells) {
      if (!cells[col].trim()) cells[col] = MERGE_LEFT;
    }
    return cells;
  });
}

function validateTableRows(
  cellRows: readonly string[][],
  columnCount: number,
  options: { allowMediumWide?: boolean } = {},
): boolean {
  const allowMediumWide = options.allowMediumWide ?? false;
  if (
    columnCount > 4 &&
    (!allowMediumWide || cellRows.length < 10) &&
    (columnCount < 8 || cellRows.length < 10)
  ) {
    return false;
  }

  const populatedCellCount = cellRows.reduce(
    (count, row) =>
      count + row.filter((cell) => cell.trim() && cell !== MERGE_LEFT).length,
    0,
  );
  if (populatedCellCount / Math.max(1, cellRows.length * columnCount) < 0.35) {
    return false;
  }

  const populatedRows = cellRows.filter(
    (row) =>
      row.filter((cell) => cell.trim() && cell !== MERGE_LEFT).length >= 2,
  );
  return populatedRows.length >= Math.max(3, Math.ceil(cellRows.length * 0.45));
}

function populatedCellCount(row: readonly string[]): number {
  return row.filter((cell) => cell.trim() && cell !== MERGE_LEFT).length;
}

function selectDenseTableBand(
  cellRows: readonly string[][],
  columnCount: number,
): string[][] {
  if (cellRows.length < 12 || columnCount < 5) {
    return cellRows.map((row) => [...row]);
  }

  const goodThreshold = Math.max(4, Math.ceil(columnCount * 0.65));
  const minGoodRows = 3;
  const maxWeakGap = 2;
  const counts = cellRows.map(populatedCellCount);
  const goodIndexes = counts
    .map((count, index) => (count >= goodThreshold ? index : -1))
    .filter((index) => index >= 0);
  if (goodIndexes.length < minGoodRows) return cellRows.map((row) => [...row]);

  type Band = { start: number; end: number; goodRows: number; score: number };
  const bands: Band[] = [];
  let start = goodIndexes[0];
  let lastGood = goodIndexes[0];
  let goodRows = 1;
  for (const index of goodIndexes.slice(1)) {
    if (index - lastGood - 1 <= maxWeakGap) {
      lastGood = index;
      goodRows += 1;
      continue;
    }
    bands.push({
      start,
      end: lastGood,
      goodRows,
      score: counts
        .slice(start, lastGood + 1)
        .reduce((sum, count) => sum + count, 0),
    });
    start = index;
    lastGood = index;
    goodRows = 1;
  }
  bands.push({
    start,
    end: lastGood,
    goodRows,
    score: counts
      .slice(start, lastGood + 1)
      .reduce((sum, count) => sum + count, 0),
  });

  const best = bands
    .filter((band) => band.goodRows >= minGoodRows)
    .sort(
      (left, right) =>
        right.score - left.score || right.goodRows - left.goodRows,
    )[0];
  if (!best) return cellRows.map((row) => [...row]);
  if (best.end - best.start + 1 >= cellRows.length * 0.85) {
    return cellRows.map((row) => [...row]);
  }
  return cellRows.slice(best.start, best.end + 1).map((row) => [...row]);
}

function looksLikeLongProseGrid(
  cellRows: readonly string[][],
  columnCount: number,
): boolean {
  if (columnCount > 3 || cellRows.length < 40) return false;

  const firstRowPopulated = populatedCellCount(cellRows[0] || []);
  if (firstRowPopulated >= columnCount) return false;

  const sparseRows = cellRows.filter(
    (row) => populatedCellCount(row) < columnCount,
  ).length;
  return sparseRows / cellRows.length >= 0.35;
}

function looksLikeSparseWidePseudoGrid(
  cellRows: readonly string[][],
  columnCount: number,
): boolean {
  if (columnCount < 5 || cellRows.length < 20) return false;

  const populatedCounts = cellRows.map(populatedCellCount);
  const populatedCells = populatedCounts.reduce((sum, count) => sum + count, 0);
  const totalCells = Math.max(1, cellRows.length * columnCount);
  const sparseRatio = 1 - populatedCells / totalCells;
  const denseThreshold = Math.max(3, Math.ceil(columnCount * 0.65));
  const denseRows = populatedCounts.filter(
    (count) => count >= denseThreshold,
  ).length;
  const sparseRowThreshold = Math.max(2, Math.floor(columnCount * 0.35));
  const verySparseRows = populatedCounts.filter(
    (count) => count <= sparseRowThreshold,
  ).length;

  return (
    sparseRatio >= 0.52 &&
    denseRows / cellRows.length < 0.25 &&
    verySparseRows / cellRows.length >= 0.35
  );
}

function nonEmptyCells(row: readonly string[]): string[] {
  return row.filter((cell) => cell.trim() && cell !== MERGE_LEFT);
}

function isCurrencyLike(value: string): boolean {
  return /(?:[€$£₽]\s*\d|\d+[.,]\d{2})/.test(value);
}

function isRatingLike(value: string): boolean {
  return /\b[0-5][.,]\d\b|\(\s*\d+\s*\)/.test(value);
}

function isDealLike(value: string): boolean {
  return /\b(?:deal|prime|exclusive|off|rrp|median|delivery|basket|options|bought|past|month|price)\b/i.test(
    value,
  );
}

function rowMatchCount(
  row: readonly string[],
  startColumn: number,
  predicate: (value: string) => boolean,
): number {
  return row.slice(startColumn).filter((cell) => predicate(cell)).length;
}

function compactJoin(parts: readonly string[]): string {
  return parts
    .map((part) => part.trim())
    .filter(Boolean)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function firstMatchingCell(
  rows: readonly string[][],
  column: number,
  predicate: (value: string) => boolean,
): string {
  return rows.find((row) => predicate(row[column] || ""))?.[column] || "";
}

interface NormalizedProductGrid {
  rows: string[][];
  prefixLines: string[];
}

function normalizeProductGridRows(
  cellRows: readonly string[][],
  columnCount: number,
): NormalizedProductGrid | null {
  if (columnCount < 5 || cellRows.length < 7) return null;

  const resultStart = columnCount >= 6 ? 1 : 0;
  const resultCount = columnCount - resultStart;
  if (resultCount < 3) return null;

  const resultColumns = Array.from(
    { length: resultCount },
    (_, index) => resultStart + index,
  );
  const ratingIndex = cellRows.findIndex(
    (row) =>
      rowMatchCount(row, resultStart, isRatingLike) >=
      Math.max(2, Math.ceil(resultCount * 0.45)),
  );
  const priceIndex = cellRows.findIndex(
    (row) =>
      rowMatchCount(row, resultStart, isCurrencyLike) >=
      Math.max(2, Math.ceil(resultCount * 0.35)),
  );
  const dealIndex = cellRows.findIndex(
    (row) =>
      rowMatchCount(row, resultStart, isDealLike) >=
      Math.max(2, Math.ceil(resultCount * 0.3)),
  );
  const firstStructuredIndex = Math.min(
    ...[ratingIndex, priceIndex, dealIndex].filter((index) => index >= 0),
  );
  if (!Number.isFinite(firstStructuredIndex) || firstStructuredIndex < 2) {
    return null;
  }

  const productRows = cellRows.slice(0, firstStructuredIndex);
  const productSignal = resultColumns.reduce(
    (count, column) =>
      count +
      (compactJoin(productRows.map((row) => row[column] || "")).length >= 24
        ? 1
        : 0),
    0,
  );
  if (productSignal < Math.max(2, Math.ceil(resultCount * 0.55))) return null;

  const structuredRows = cellRows.slice(firstStructuredIndex);
  const ratingRow = ratingIndex >= 0 ? cellRows[ratingIndex] : undefined;
  const priceRows = structuredRows.filter(
    (row) => rowMatchCount(row, resultStart, isCurrencyLike) > 0,
  );
  const dealRows = structuredRows.filter(
    (row) => rowMatchCount(row, resultStart, isDealLike) > 0,
  );
  const boughtRows = structuredRows.filter(
    (row) =>
      rowMatchCount(row, resultStart, isCurrencyLike) === 0 &&
      rowMatchCount(row, resultStart, isDealLike) === 0 &&
      row !== ratingRow &&
      nonEmptyCells(row.slice(resultStart)).length >= 2,
  );

  const prefixLines =
    resultStart > 0
      ? cellRows
          .map((row) => row[0] || "")
          .map((cell) => compactJoin([cell]))
          .filter(Boolean)
      : [];

  const output = [
    ["Field", ...resultColumns.map((_, index) => `Result ${index + 1}`)],
    ["Badge", ...resultColumns.map(() => "")],
    [
      "Product",
      ...resultColumns.map((column) =>
        compactJoin(productRows.map((row) => row[column] || "")),
      ),
    ],
    [
      "Rating",
      ...resultColumns.map((column) => ratingRow?.[column]?.trim() || ""),
    ],
    [
      "Bought",
      ...resultColumns.map((column) =>
        compactJoin(boughtRows.map((row) => row[column] || "")),
      ),
    ],
    [
      "Deal",
      ...resultColumns.map((column) =>
        compactJoin(
          dealRows
            .map((row) => row[column] || "")
            .filter((cell) => !isCurrencyLike(cell)),
        ),
      ),
    ],
    [
      "Price",
      ...resultColumns.map((column) =>
        firstMatchingCell(priceRows, column, isCurrencyLike),
      ),
    ],
    [
      "Extra",
      ...resultColumns.map((column) =>
        compactJoin(
          dealRows
            .map((row) => row[column] || "")
            .filter(
              (cell) =>
                isDealLike(cell) && !/\b(?:deal|basket|options)\b/i.test(cell),
            ),
        ),
      ),
    ],
    [
      "Action",
      ...resultColumns.map((column) =>
        firstMatchingCell(structuredRows, column, (cell) =>
          /\b(?:options|basket)\b/i.test(cell),
        ),
      ),
    ],
  ];

  return { rows: output, prefixLines };
}

function tableRowsToMarkdown(
  rows: string[][],
  preserveEmptyRows = false,
): string {
  const nonEmptyRows = preserveEmptyRows
    ? rows
    : rows.filter((row) => row.some((cell) => cell.trim()));
  if (nonEmptyRows.length === 0) return "";
  const maxCols = Math.max(...nonEmptyRows.map((row) => row.length));
  const paddedRows = nonEmptyRows.map((row) => [
    ...row,
    ...Array<string>(maxCols - row.length).fill(""),
  ]);
  const header = paddedRows[0].map(
    (cell, index) => cell || `Column ${index + 1}`,
  );
  const separator = header.map(() => "---");
  const body = paddedRows.slice(1);
  return [
    header,
    separator,
    ...(body.length > 0 ? body : [header.map(() => " ")]),
  ]
    .map((row) => `| ${row.join(" | ")} |`)
    .join("\n");
}

function tableMarkdownForLines(
  rows: readonly SlotWord[][],
  xLines: readonly number[],
  options: {
    maxColumns: number;
    allowMediumWide?: boolean;
  },
): string {
  if (xLines.length < 4) return "";
  const columnCount = xLines.length - 1;
  if (columnCount > options.maxColumns) return "";

  const cellRows = tableCellsForLines(rows, xLines);
  if (looksLikeLongProseGrid(cellRows, columnCount)) return "";
  if (looksLikeSparseWidePseudoGrid(cellRows, columnCount)) return "";

  const tableRows = options.allowMediumWide
    ? selectDenseTableBand(cellRows, columnCount)
    : cellRows;
  if (looksLikeSparseWidePseudoGrid(tableRows, columnCount)) return "";
  const normalizedRows =
    options.allowMediumWide && normalizeProductGridRows(tableRows, columnCount);
  if (normalizedRows) {
    return [
      ...normalizedRows.prefixLines,
      tableRowsToMarkdown(normalizedRows.rows),
    ]
      .filter(Boolean)
      .join("\n");
  }

  if (
    !validateTableRows(tableRows, columnCount, {
      allowMediumWide: options.allowMediumWide,
    })
  ) {
    return "";
  }

  return tableRowsToMarkdown(tableRows);
}

function coarseColumnLines(
  rows: readonly SlotWord[][],
  words: readonly SlotWord[],
  maxColumns: number,
): number[] {
  if (maxColumns < 5 || rows.length < 4 || words.length < 12) return [];

  const widths = words.map((word) => word.right - word.left);
  const heights = words.map((word) => word.bottom - word.top);
  const left = Math.min(...words.map((word) => word.left));
  const right = Math.max(...words.map((word) => word.right));
  const tableWidth = Math.max(1, right - left);
  const denseRowThreshold = Math.min(
    8,
    Math.max(4, Math.floor(maxColumns * 0.55)),
  );
  const denseRows = rows.filter((row) => row.length >= denseRowThreshold);
  if (denseRows.length < 4) return [];

  const minGap = Math.max(
    14,
    median(widths, 24) * 0.9,
    median(heights, 12) * 1.4,
    tableWidth * 0.012,
  );
  const gaps: Array<{ center: number; gap: number; row: number }> = [];
  denseRows.forEach((row, rowIndex) => {
    for (let index = 1; index < row.length; index += 1) {
      const gap = row[index].left - row[index - 1].right;
      if (gap >= minGap) {
        gaps.push({
          center: (row[index].left + row[index - 1].right) / 2,
          gap,
          row: rowIndex,
        });
      }
    }
  });
  if (gaps.length < 2) return [];

  const mergeTolerance = Math.max(12, median(widths, 24) * 0.8);
  const clusters: Array<{
    center: number;
    gaps: typeof gaps;
    rows: Set<number>;
  }> = [];
  for (const gap of gaps.sort((a, b) => a.center - b.center)) {
    const current = clusters[clusters.length - 1];
    if (!current || gap.center - current.center > mergeTolerance) {
      clusters.push({
        center: gap.center,
        gaps: [gap],
        rows: new Set([gap.row]),
      });
      continue;
    }
    current.gaps.push(gap);
    current.rows.add(gap.row);
    current.center =
      current.gaps.reduce((sum, item) => sum + item.center, 0) /
      current.gaps.length;
  }

  const supportMin = Math.max(2, Math.ceil(denseRows.length * 0.22));
  const minSeparation = Math.max(
    60,
    tableWidth / Math.max(6, maxColumns * 1.25),
  );
  const chosen: Array<{ center: number; score: number }> = [];
  const candidates = clusters
    .map((cluster) => {
      const avgGap =
        cluster.gaps.reduce((sum, gap) => sum + gap.gap, 0) /
        cluster.gaps.length;
      return {
        center: Math.round(cluster.center),
        support: cluster.rows.size,
        score: cluster.rows.size * 100 + avgGap,
      };
    })
    .filter((cluster) => cluster.support >= supportMin)
    .sort((a, b) => b.score - a.score);

  for (const candidate of candidates) {
    if (
      chosen.every(
        (existing) =>
          Math.abs(existing.center - candidate.center) >= minSeparation,
      )
    ) {
      chosen.push(candidate);
    }
    if (chosen.length >= maxColumns - 1) break;
  }
  if (chosen.length < 2) return [];

  return mergeLines(
    [left, right, ...chosen.map((candidate) => candidate.center)],
    Math.max(3, tableWidth * 0.003),
  );
}

export function browserWordsToTableMarkdown(
  result: BrowserOcrDetailedResult,
  options: BrowserTableSlotOptions = {},
): string {
  const words = result.words
    .map(toSlotWord)
    .filter((word): word is SlotWord => word !== null);
  if (words.length < 8) return "";

  const rows = groupRows(words);
  if (rows.length < 3) return "";

  const widths = words.map((word) => word.right - word.left);
  const heights = words.map((word) => word.bottom - word.top);
  const left = Math.min(...words.map((word) => word.left));
  const right = Math.max(...words.map((word) => word.right));
  const tableWidth = Math.max(1, right - left);
  const minGap = Math.max(
    16,
    median(widths, 24) * 0.55,
    median(heights, 12) * 1.2,
    tableWidth * 0.012,
  );
  const xLines = mergeLines(
    [left, right, ...rows.flatMap((row) => recursiveCuts(row, minGap))],
    Math.max(3, tableWidth * 0.003),
  );
  const maxColumns = options.maxColumns ?? 4;
  const fineMarkdown = tableMarkdownForLines(rows, xLines, { maxColumns });
  if (fineMarkdown) return fineMarkdown;

  const coarseLines = coarseColumnLines(rows, words, maxColumns);
  return tableMarkdownForLines(rows, coarseLines, {
    maxColumns,
    allowMediumWide: true,
  });
}

export async function browserWordsToReviewedTableMarkdown(
  result: BrowserOcrDetailedResult,
  options: BrowserTableSlotOptions,
  review: (candidate: BrowserTextCandidate) => Promise<boolean>,
): Promise<string> {
  const markdown = browserWordsToTableMarkdown(result, options);
  if (!markdown) return "";

  const rows = markdown.split(/\r?\n/);
  const nonTableRows = rows.filter((line) => !line.trim().startsWith("|"));
  const tableRows = rows
    .filter((line) => line.startsWith("|") && !/^\|\s*---/.test(line))
    .map((line) =>
      line
        .slice(1, -1)
        .split("|")
        .map((cell) => cell.trim()),
    );
  if (!tableRows.length) return markdown;

  const reviewed = tableRows.map((row) => [...row]);
  for (let rowIndex = 0; rowIndex < reviewed.length; rowIndex += 1) {
    for (
      let colIndex = 0;
      colIndex < reviewed[rowIndex].length;
      colIndex += 1
    ) {
      const text = reviewed[rowIndex][colIndex];
      if (!text || text === MERGE_LEFT || /^Column \d+$/.test(text)) continue;
      const matchingConfidences = result.words
        .filter((word) => text.includes(cleanWord(word.text)))
        .map((word) => word.confidence)
        .filter(
          (confidence): confidence is number => typeof confidence === "number",
        );
      const isText = await review({
        text,
        left: reviewed[rowIndex][colIndex - 1] || "",
        top: reviewed[rowIndex - 1]?.[colIndex] || "",
        confidence:
          matchingConfidences.length > 0
            ? Math.min(...matchingConfidences)
            : null,
      });
      if (!isText) reviewed[rowIndex][colIndex] = "";
    }
  }
  return [...nonTableRows, tableRowsToMarkdown(reviewed, true)]
    .filter(Boolean)
    .join("\n");
}
