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
  if (xLines.length < 4) return "";
  const columnCount = xLines.length - 1;
  if (columnCount > (options.maxColumns ?? 4)) return "";
  if (columnCount > 4 && (columnCount < 8 || rows.length < 10)) return "";

  const cellRows = rows.map((rowWords) => {
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

  const populatedCellCount = cellRows.reduce(
    (count, row) =>
      count + row.filter((cell) => cell.trim() && cell !== MERGE_LEFT).length,
    0,
  );
  if (populatedCellCount / Math.max(1, cellRows.length * columnCount) < 0.35) {
    return "";
  }

  const populatedRows = cellRows.filter(
    (row) =>
      row.filter((cell) => cell.trim() && cell !== MERGE_LEFT).length >= 2,
  );
  if (populatedRows.length < Math.max(3, Math.ceil(cellRows.length * 0.45)))
    return "";

  return tableRowsToMarkdown(cellRows);
}

export async function browserWordsToReviewedTableMarkdown(
  result: BrowserOcrDetailedResult,
  options: BrowserTableSlotOptions,
  review: (candidate: BrowserTextCandidate) => Promise<boolean>,
): Promise<string> {
  const markdown = browserWordsToTableMarkdown(result, options);
  if (!markdown) return "";

  const rows = markdown.split(/\r?\n/);
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
  return tableRowsToMarkdown(reviewed, true);
}
