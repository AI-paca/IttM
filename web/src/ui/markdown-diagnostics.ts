export interface MarkdownGrammarDiagnostics {
  errors: string[];
  maxColumns: number;
  mergeMarkers: number;
  status: "pass" | "warn";
  tableCount: number;
  tableRows: number;
  warnings: string[];
}

const MERGE_MARKERS = new Set([
  "::merge-left::",
  "::merge-up::",
  "::merge-up-left::",
]);

function splitMarkdownRow(line: string): string[] {
  const trimmed = line.trim();
  const body =
    trimmed.startsWith("|") && trimmed.endsWith("|")
      ? trimmed.slice(1, -1)
      : trimmed;
  return body.split("|").map((cell) => cell.trim());
}

function isTableSeparator(line: string): boolean {
  const cells = splitMarkdownRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function isPipeRow(line: string): boolean {
  return line.includes("|") && splitMarkdownRow(line).length > 1;
}

export function analyzeMarkdownGrammar(
  markdown: string,
): MarkdownGrammarDiagnostics {
  const errors: string[] = [];
  const warnings: string[] = [];
  let inFence = false;
  let tableCount = 0;
  let tableRows = 0;
  let maxColumns = 0;
  let mergeMarkers = 0;

  const lines = markdown.split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    const lineNumber = index + 1;

    if (/^```/.test(trimmed)) {
      inFence = !inFence;
      continue;
    }
    if (inFence || !trimmed) continue;

    if (/^#{7,}\s/.test(trimmed)) {
      errors.push(`line ${lineNumber}: heading level above 6`);
    }
    if (/^[-*+]\s*$/.test(trimmed)) {
      errors.push(`line ${lineNumber}: empty list item`);
    }

    if (!isPipeRow(trimmed)) continue;

    const nextLine = lines[index + 1]?.trim() ?? "";
    if (!isTableSeparator(nextLine)) {
      warnings.push(`line ${lineNumber}: pipe row without separator`);
      continue;
    }

    tableCount += 1;
    const expectedColumns = splitMarkdownRow(trimmed).length;
    maxColumns = Math.max(maxColumns, expectedColumns);
    tableRows += 1;
    index += 1;
    tableRows += 1;

    for (index += 1; index < lines.length; index += 1) {
      const row = lines[index].trim();
      if (!row || !isPipeRow(row)) {
        index -= 1;
        break;
      }
      const cells = splitMarkdownRow(row);
      tableRows += 1;
      maxColumns = Math.max(maxColumns, cells.length);
      mergeMarkers += cells.filter((cell) => MERGE_MARKERS.has(cell)).length;
      if (cells.length !== expectedColumns) {
        errors.push(
          `line ${index + 1}: table row has ${cells.length} cells, expected ${expectedColumns}`,
        );
      }
    }
  }

  if (inFence) {
    errors.push("unclosed fenced code block");
  }

  return {
    errors,
    maxColumns,
    mergeMarkers,
    status: errors.length ? "warn" : "pass",
    tableCount,
    tableRows,
    warnings,
  };
}
