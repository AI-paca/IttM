import type { BrowserPipelineCore } from "./pipeline-core";
import {
  normalizeOcrText,
  normalizedOcrTokens,
  ocrCompactCharCount,
  sharedOcrTokenCount,
} from "./text-block-metrics";

function normalizeLine(value: string): string {
  return normalizeOcrText(value);
}

type TextBlockDeduplicator = Pick<BrowserPipelineCore, "shouldDropTextBlock">;

interface OcrTextBlock {
  end: number;
  lines: string[];
  normalized: string;
  compactChars: number;
  tokens: string[];
}

function ocrTextBlocks(lines: readonly string[]): OcrTextBlock[] {
  const blocks: OcrTextBlock[] = [];
  let start = -1;

  const addBlock = (end: number) => {
    if (start < 0) return;
    const blockLines = lines.slice(start, end);
    const text = blockLines.join("\n");
    blocks.push({
      end,
      lines: blockLines,
      normalized: normalizeOcrText(text),
      compactChars: ocrCompactCharCount(text),
      tokens: normalizedOcrTokens(text),
    });
    start = -1;
  };

  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].trim()) {
      if (start < 0) start = index;
    } else {
      addBlock(index);
    }
  }
  addBlock(lines.length);
  return blocks;
}

function isDuplicateTextBlock(
  candidate: OcrTextBlock,
  existing: OcrTextBlock,
  core: TextBlockDeduplicator,
): boolean {
  const exactNormalized = candidate.normalized === existing.normalized;
  if (
    !exactNormalized &&
    candidate.lines.length === 1 &&
    existing.lines.length === 1
  ) {
    return false;
  }

  return core.shouldDropTextBlock({
    candidateChars: candidate.compactChars,
    existingChars: existing.compactChars,
    sharedTokens: sharedOcrTokenCount(candidate.tokens, existing.tokens),
    candidateTokens: candidate.tokens.length,
    existingTokens: existing.tokens.length,
    similarityMilli: exactNormalized ? 1_000 : 0,
  });
}

function appendDeduplicatedTextBlocks(
  output: string[],
  incoming: readonly string[],
  core: TextBlockDeduplicator,
): void {
  const existingBlocks = ocrTextBlocks(output);
  const incomingBlocks = ocrTextBlocks(incoming);
  let cursor = 0;
  let retainedBlock = false;

  for (const candidate of incomingBlocks) {
    const duplicate = existingBlocks.some((existing) =>
      isDuplicateTextBlock(candidate, existing, core),
    );
    if (duplicate) {
      cursor = candidate.end;
      continue;
    }
    output.push(...incoming.slice(cursor, candidate.end));
    existingBlocks.push(candidate);
    cursor = candidate.end;
    retainedBlock = true;
  }

  if (retainedBlock) output.push(...incoming.slice(cursor));
}

function markdownColumnCount(line: string): number {
  if (!line.startsWith("|")) return 0;
  return Math.max(0, line.split("|").length - 2);
}

function markdownCells(line: string): string[] {
  if (!line.startsWith("|")) return [];
  return line
    .slice(1, -1)
    .split("|")
    .map((cell) => cell.trim());
}

function markdownSparseTablePenalty(tableRows: readonly string[][]): number {
  const rows = tableRows.length;
  const maxColumns = Math.max(0, ...tableRows.map((row) => row.length));
  if (rows < 20 || maxColumns < 5) return 0;

  const populatedCounts = tableRows.map(
    (row) =>
      row.filter(
        (cell) =>
          cell && cell !== "::merge-left::" && !/^Column \d+$/i.test(cell),
      ).length,
  );
  const populatedCells = populatedCounts.reduce((sum, count) => sum + count, 0);
  const totalCells = Math.max(1, rows * maxColumns);
  const sparseRatio = 1 - populatedCells / totalCells;
  const denseThreshold = Math.max(3, Math.ceil(maxColumns * 0.65));
  const denseRows = populatedCounts.filter(
    (count) => count >= denseThreshold,
  ).length;
  const sparseRowThreshold = Math.max(2, Math.floor(maxColumns * 0.35));
  const verySparseRows = populatedCounts.filter(
    (count) => count <= sparseRowThreshold,
  ).length;

  if (
    sparseRatio < 0.52 ||
    denseRows / rows >= 0.25 ||
    verySparseRows / rows < 0.35
  ) {
    return 0;
  }

  return 900 + rows * maxColumns * (sparseRatio - 0.42) * 5;
}

function ocrChunkScore(text: string): number {
  const lines = text.split(/\r?\n/);
  const tableRows = lines.filter(
    (line) => line.startsWith("|") && !/^\|\s*:?-{3,}/.test(line),
  );
  const tableCells = tableRows.map(markdownCells);
  const maxColumns = Math.max(0, ...tableRows.map(markdownColumnCount));
  const tableScore =
    tableRows.length >= 3
      ? 220 + Math.min(80, tableRows.length) * 4 + maxColumns * 18
      : 0;
  const tokens = text.match(/[\p{L}\p{N}_]{2,}/gu) ?? [];
  const usefulCharacters = tokens.join("").length;
  const noisyCharacters = Array.from(text).filter(
    (character) =>
      !/[\p{L}\p{N}\s]/u.test(character) &&
      !".,;:!?+-*/=<>^_()[]{}№%$€₽'\"|:-".includes(character),
  ).length;
  return (
    tableScore +
    tokens.length * 2 +
    usefulCharacters * 0.2 -
    noisyCharacters -
    markdownSparseTablePenalty(tableCells)
  );
}

export function selectBetterOcrChunk(left: string, right: string): string {
  if (!left.trim()) return right;
  if (!right.trim()) return left;
  return ocrChunkScore(right) > ocrChunkScore(left) * 1.03 ? right : left;
}

export function mergeOcrTextChunks(
  chunks: readonly string[],
  core?: TextBlockDeduplicator,
): string {
  const output: string[] = [];

  for (const chunk of chunks) {
    const incoming = chunk.split(/\r?\n/);
    const normalizedOutput = output.map(normalizeLine);
    const normalizedIncoming = incoming.map(normalizeLine);
    const maxOverlap = Math.min(
      20,
      normalizedOutput.length,
      normalizedIncoming.length,
    );
    let overlap = 0;

    for (let size = maxOverlap; size > 0; size -= 1) {
      const left = normalizedOutput.slice(-size);
      const right = normalizedIncoming.slice(0, size);
      if (
        left.every((line, index) => line.length > 0 && line === right[index])
      ) {
        overlap = size;
        break;
      }
    }
    const remaining = incoming.slice(overlap);
    if (core) {
      appendDeduplicatedTextBlocks(output, remaining, core);
    } else {
      output.push(...remaining);
    }
  }

  return output.join("\n").trim();
}
