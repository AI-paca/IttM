import {
  assembleSegmentTopologyHandoff,
  SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
  type SegmentTopologyHandoff,
  type StructuralObjectKind,
  type StructuralRenderArtifact,
} from "../ocr/segment-assembler";

export const PDF_NATIVE_ORACLE_SCHEMA = "ittm.pdf-native-oracle/v2";

export interface PdfTextGeometryItem {
  str?: string;
  hasEOL?: boolean;
  width?: number;
  height?: number;
  transform?: readonly number[];
}

export interface OracleBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface NativeTextCandidate {
  sourceItemIndex: number;
  bbox: OracleBox;
  text: string;
}

export interface NativeTextObject {
  objectId: string;
  kind: StructuralObjectKind;
  bbox: OracleBox;
  rows: number[][];
  cells: NativeTextCell[];
}

export interface NativeTextCell {
  row: number;
  column: number;
  rowSpan: number;
  columnSpan: number;
  bbox: OracleBox;
  candidateIndexes: number[];
}

export interface NativeExtractedSegment {
  segmentId: string;
  sourceItemIndex: number;
  bbox: OracleBox;
  topology: SegmentTopologyHandoff["segments"][number]["topology"];
  text: string;
}

export interface NativePdfOracle {
  schema: typeof PDF_NATIVE_ORACLE_SCHEMA;
  source: "trusted_pdf_text_layer";
  boundaryBasis: "pdfjs_text_item_bbox";
  nativeFindObject: { objects: NativeTextObject[] };
  nativeSegmentExtraction: { segments: NativeExtractedSegment[] };
  assemblerHandoff: SegmentTopologyHandoff;
  assembled: StructuralRenderArtifact;
}

function candidateFromItem(
  item: PdfTextGeometryItem,
  sourceItemIndex: number,
): NativeTextCandidate | null {
  const text = (item.str ?? "").trim();
  if (!text) return null;
  const transform = item.transform ?? [];
  const x = Number(transform[4] ?? 0);
  const baseline = Number(transform[5] ?? 0);
  const width = Math.max(1, Math.abs(Number(item.width ?? 0)));
  const transformHeight = Math.hypot(
    Number(transform[2] ?? 0),
    Number(transform[3] ?? 0),
  );
  const height = Math.max(1, Math.abs(Number(item.height ?? transformHeight)));
  return {
    sourceItemIndex,
    bbox: {
      left: Math.round(x),
      top: Math.round(baseline - height),
      right: Math.round(x + width),
      bottom: Math.round(baseline),
    },
    text,
  };
}

interface NativeRow {
  centerY: number;
  height: number;
  bbox: OracleBox;
  candidateIndexes: number[];
}

function unionBoxes(boxes: readonly OracleBox[]): OracleBox {
  return {
    left: Math.min(...boxes.map((box) => box.left)),
    top: Math.min(...boxes.map((box) => box.top)),
    right: Math.max(...boxes.map((box) => box.right)),
    bottom: Math.max(...boxes.map((box) => box.bottom)),
  };
}

function median(values: readonly number[]): number {
  if (values.length === 0) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 0
    ? (ordered[middle - 1] + ordered[middle]) / 2
    : ordered[middle];
}

function alignedColumnCount(
  left: NativeRow,
  right: NativeRow,
  candidates: readonly NativeTextCandidate[],
): number {
  const tolerance = Math.max(3, Math.min(left.height, right.height) * 0.8);
  const available = new Set(right.candidateIndexes);
  let matches = 0;
  for (const leftIndex of left.candidateIndexes) {
    const leftX = candidates[leftIndex].bbox.left;
    let nearest: number | null = null;
    let nearestDistance = Number.POSITIVE_INFINITY;
    for (const rightIndex of available) {
      const distance = Math.abs(candidates[rightIndex].bbox.left - leftX);
      if (distance <= tolerance && distance < nearestDistance) {
        nearest = rightIndex;
        nearestDistance = distance;
      }
    }
    if (nearest === null) continue;
    available.delete(nearest);
    matches += 1;
  }
  return matches;
}

function rowsAreAdjacent(left: NativeRow, right: NativeRow): boolean {
  return (
    Math.abs(left.centerY - right.centerY) <=
    Math.max(18, Math.max(left.height, right.height) * 3)
  );
}

function rowContinuesTable(
  row: NativeRow,
  tableBox: OracleBox,
  pageWidth: number,
): boolean {
  const margin = Math.max(12, pageWidth * 0.03);
  const rowWidth = row.bbox.right - row.bbox.left;
  const tableWidth = tableBox.right - tableBox.left;
  const overlap =
    Math.min(row.bbox.right, tableBox.right) -
    Math.max(row.bbox.left, tableBox.left);
  const leftAligned =
    row.bbox.left <= tableBox.left + margin &&
    row.bbox.right >= tableBox.left - margin;
  const broad =
    overlap > 0 &&
    rowWidth >= tableWidth * 0.45 &&
    overlap * 2 >= rowWidth;
  return leftAligned || broad;
}

function rowAlignsWithEstablishedTableColumn(
  row: NativeRow,
  tableRows: readonly NativeRow[],
  candidates: readonly NativeTextCandidate[],
): boolean {
  if (row.candidateIndexes.length !== 1 || tableRows.length < 2) return false;
  const candidate = candidates[row.candidateIndexes[0]];
  const tolerance = Math.max(3, row.height * 0.8);
  const supportingRows = tableRows.filter((tableRow) =>
    tableRow.candidateIndexes.some(
      (candidateIndex) =>
        Math.abs(candidates[candidateIndex].bbox.left - candidate.bbox.left) <=
        tolerance,
    ),
  ).length;
  return supportingRows >= 2;
}

interface NativeRowRange {
  start: number;
  end: number;
}

function findTableRanges(
  rows: readonly NativeRow[],
  candidates: readonly NativeTextCandidate[],
): NativeRowRange[] {
  if (rows.length < 2) return [];
  const pageBox = unionBoxes(candidates.map((candidate) => candidate.bbox));
  const pageWidth = Math.max(1, pageBox.right - pageBox.left);
  const tableSeeds = rows.map((row, index) => {
    if (row.candidateIndexes.length >= 4) return true;
    if (row.candidateIndexes.length < 2) return false;
    return [rows[index - 1], rows[index + 1]].some(
      (neighbor) =>
        neighbor !== undefined &&
        neighbor.candidateIndexes.length >= 2 &&
        rowsAreAdjacent(row, neighbor) &&
        alignedColumnCount(row, neighbor, candidates) >= 2,
    );
  });

  const ranges: NativeRowRange[] = [];
  let cursor = 0;
  while (cursor < rows.length) {
    while (cursor < rows.length && !tableSeeds[cursor]) cursor += 1;
    if (cursor >= rows.length) break;
    let start = cursor;
    let end = cursor;
    let tableBox = rows[cursor].bbox;
    let seedCount = 0;
    let index = cursor;
    for (; index < rows.length; index += 1) {
      if (index > cursor && !rowsAreAdjacent(rows[index - 1], rows[index])) {
        break;
      }
      const isSandwiched =
        index > cursor &&
        index + 1 < rows.length &&
        tableSeeds[index - 1] &&
        tableSeeds[index + 1] &&
        rowsAreAdjacent(rows[index], rows[index + 1]);
      if (
        !tableSeeds[index] &&
        !isSandwiched &&
        !rowContinuesTable(rows[index], tableBox, pageWidth)
      ) {
        break;
      }
      if (tableSeeds[index]) seedCount += 1;
      end = index;
      tableBox = unionBoxes([tableBox, rows[index].bbox]);
    }
    if (seedCount < 2) {
      cursor += 1;
      continue;
    }
    while (
      start > 0 &&
      !tableSeeds[start - 1] &&
      rowsAreAdjacent(rows[start - 1], rows[start]) &&
      rowContinuesTable(rows[start - 1], tableBox, pageWidth)
    ) {
      start -= 1;
      tableBox = unionBoxes([tableBox, rows[start].bbox]);
    }
    ranges.push({ start, end });
    cursor = Math.max(index, end + 1);
  }
  return ranges;
}

interface ColumnAnchor {
  position: number;
  observations: number;
  rowIndexes: Set<number>;
}

function inferTableCells(
  rows: readonly NativeRow[],
  candidates: readonly NativeTextCandidate[],
): NativeTextCell[] {
  const tolerance = Math.max(2, median(rows.map((row) => row.height)) * 0.8);
  const anchors: ColumnAnchor[] = [];
  rows.forEach((row, rowIndex) => {
    for (const candidateIndex of row.candidateIndexes) {
      const x = candidates[candidateIndex].bbox.left;
      let anchor = anchors
        .filter((value) => Math.abs(value.position - x) <= tolerance)
        .sort(
          (left, right) =>
            Math.abs(left.position - x) - Math.abs(right.position - x),
        )[0];
      if (!anchor) {
        anchor = {
          position: x,
          observations: 0,
          rowIndexes: new Set<number>(),
        };
        anchors.push(anchor);
      }
      anchor.position =
        (anchor.position * anchor.observations + x) /
        (anchor.observations + 1);
      anchor.observations += 1;
      anchor.rowIndexes.add(rowIndex);
    }
  });
  const minimumSupport = Math.max(2, Math.ceil(rows.length * 0.08));
  let supportedAnchors = anchors.filter(
    (anchor) => anchor.rowIndexes.size >= minimumSupport,
  );
  if (supportedAnchors.length < 2) {
    supportedAnchors = [...anchors]
      .sort(
        (left, right) =>
          right.rowIndexes.size - left.rowIndexes.size ||
          left.position - right.position,
      )
      .slice(0, Math.max(2, Math.max(...rows.map((row) => row.candidateIndexes.length))));
  }
  supportedAnchors.sort((left, right) => left.position - right.position);

  const cells: NativeTextCell[] = [];
  rows.forEach((row, rowIndex) => {
    const grouped = new Map<number, number[]>();
    for (const candidateIndex of row.candidateIndexes) {
      const candidate = candidates[candidateIndex];
      let column = 0;
      for (let index = 1; index < supportedAnchors.length; index += 1) {
        if (
          Math.abs(supportedAnchors[index].position - candidate.bbox.left) <
          Math.abs(supportedAnchors[column].position - candidate.bbox.left)
        ) {
          column = index;
        }
      }
      const indexes = grouped.get(column) ?? [];
      indexes.push(candidateIndex);
      grouped.set(column, indexes);
    }
    for (const [column, candidateIndexes] of grouped) {
      candidateIndexes.sort(
        (left, right) =>
          candidates[left].bbox.left - candidates[right].bbox.left ||
          candidates[left].sourceItemIndex - candidates[right].sourceItemIndex,
      );
      const bbox = unionBoxes(
        candidateIndexes.map((candidateIndex) => candidates[candidateIndex].bbox),
      );
      let lastColumn = column;
      while (
        lastColumn + 1 < supportedAnchors.length &&
        !grouped.has(lastColumn + 1) &&
        supportedAnchors[lastColumn + 1].position < bbox.right - tolerance
      ) {
        lastColumn += 1;
      }
      cells.push({
        row: rowIndex,
        column,
        // A PDF text item bbox describes glyph ink, not a physical cell
        // boundary. Adjacent glyph boxes commonly overlap vertically, so
        // using their height as rowspan evidence corrupts the next row.
        rowSpan: 1,
        columnSpan: lastColumn - column + 1,
        bbox,
        candidateIndexes,
      });
    }
  });
  return cells.sort(
    (left, right) => left.row - right.row || left.column - right.column,
  );
}

function paragraphCells(
  rows: readonly NativeRow[],
  candidates: readonly NativeTextCandidate[],
): NativeTextCell[] {
  return rows.map((row, rowIndex) => ({
    row: rowIndex,
    column: 0,
    rowSpan: 1,
    columnSpan: 1,
    bbox: row.bbox,
    candidateIndexes: [...row.candidateIndexes].sort(
      (left, right) =>
        candidates[left].bbox.left - candidates[right].bbox.left ||
        candidates[left].sourceItemIndex - candidates[right].sourceItemIndex,
    ),
  }));
}

// Native PDF owns this object detector. It uses only text-layer bbox geometry.
export function findNativePdfTextObjects(
  candidates: readonly NativeTextCandidate[],
): NativeTextObject[] {
  const rows: NativeRow[] = [];
  const orderedIndexes = candidates
    .map((_candidate, index) => index)
    .sort((leftIndex, rightIndex) => {
      const left = candidates[leftIndex];
      const right = candidates[rightIndex];
      return (
        right.bbox.bottom - left.bbox.bottom ||
        left.bbox.left - right.bbox.left ||
        left.sourceItemIndex - right.sourceItemIndex
      );
    });

  for (const candidateIndex of orderedIndexes) {
    const candidate = candidates[candidateIndex];
    const height = candidate.bbox.bottom - candidate.bbox.top;
    const centerY = (candidate.bbox.top + candidate.bbox.bottom) / 2;
    const row = rows.find(
      (value) =>
        Math.abs(value.centerY - centerY) <=
        Math.max(2, Math.min(value.height, height) * 0.6),
    );
    if (row) {
      row.candidateIndexes.push(candidateIndex);
      row.centerY =
        row.candidateIndexes.reduce((sum, index) => {
          const value = candidates[index].bbox;
          return sum + (value.top + value.bottom) / 2;
        }, 0) / row.candidateIndexes.length;
      row.height = Math.max(row.height, height);
      row.bbox = unionBoxes([row.bbox, candidate.bbox]);
    } else {
      rows.push({
        centerY,
        height,
        bbox: candidate.bbox,
        candidateIndexes: [candidateIndex],
      });
    }
  }

  rows.sort((left, right) => right.centerY - left.centerY);
  for (const row of rows) {
    row.candidateIndexes.sort(
      (left, right) => candidates[left].bbox.left - candidates[right].bbox.left,
    );
  }
  const tableRanges = findTableRanges(rows, candidates);
  const tableByStart = new Map(tableRanges.map((range) => [range.start, range]));
  const objectDrafts: Array<{
    kind: StructuralObjectKind;
    rows: NativeRow[];
  }> = [];
  let rowIndex = 0;
  while (rowIndex < rows.length) {
    const tableRange = tableByStart.get(rowIndex);
    if (tableRange) {
      const objectRows = rows.slice(tableRange.start, tableRange.end + 1);
      const maximumColumns = Math.max(
        ...objectRows.map((row) => row.candidateIndexes.length),
      );
      objectDrafts.push({
        kind: "table",
        rows: objectRows,
      });
      rowIndex = tableRange.end + 1;
      continue;
    }
    const start = rowIndex;
    rowIndex += 1;
    while (
      rowIndex < rows.length &&
      !tableByStart.has(rowIndex) &&
      rowsAreAdjacent(rows[rowIndex - 1], rows[rowIndex])
    ) {
      rowIndex += 1;
    }
    objectDrafts.push({ kind: "paragraph", rows: rows.slice(start, rowIndex) });
  }

  for (let index = 1; index + 1 < objectDrafts.length; ) {
    const previous = objectDrafts[index - 1];
    const continuation = objectDrafts[index];
    const next = objectDrafts[index + 1];
    const continuationRow = continuation.rows[0];
    const previousBox = unionBoxes(previous.rows.map((row) => row.bbox));
    const nextBox = unionBoxes(next.rows.map((row) => row.bbox));
    const overlapsPrevious =
      Math.min(continuationRow.bbox.bottom, previousBox.bottom) -
      Math.max(continuationRow.bbox.top, previousBox.top);
    const overlapsNext =
      Math.min(continuationRow.bbox.bottom, nextBox.bottom) -
      Math.max(continuationRow.bbox.top, nextBox.top);
    const belongsToPreviousTable =
      previous.kind === "table" &&
      continuation.kind === "paragraph" &&
      continuation.rows.length === 1 &&
      next.kind === "table" &&
      overlapsPrevious > 0 &&
      overlapsNext <= 0 &&
      rowAlignsWithEstablishedTableColumn(
        continuationRow,
        previous.rows,
        candidates,
      );
    if (!belongsToPreviousTable) {
      index += 1;
      continue;
    }
    previous.rows.push(continuationRow);
    objectDrafts.splice(index, 1);
  }

  return objectDrafts.map((draft, objectIndex) => {
    const bbox = unionBoxes(draft.rows.map((row) => row.bbox));
    return {
      objectId: `native-object-${objectIndex + 1}`,
      kind: draft.kind,
      bbox,
      rows: draft.rows.map((row) => [...row.candidateIndexes]),
      cells:
        draft.kind === "paragraph"
          ? paragraphCells(draft.rows, candidates)
          : inferTableCells(draft.rows, candidates),
    };
  });
}

// Native PDF owns this segment extractor. Raster code must not call it.
export function extractNativePdfTextSegments(
  candidates: readonly NativeTextCandidate[],
  objects: readonly NativeTextObject[],
): NativeExtractedSegment[] {
  const segments: NativeExtractedSegment[] = [];
  for (const object of objects) {
    for (const cell of object.cells) {
      for (const candidateIndex of cell.candidateIndexes) {
        const candidate = candidates[candidateIndex];
        segments.push({
          segmentId: `native-segment-${String(candidate.sourceItemIndex + 1).padStart(6, "0")}`,
          sourceItemIndex: candidate.sourceItemIndex,
          bbox: candidate.bbox,
          topology: {
            object_id: object.objectId,
            object_kind: object.kind,
            row: cell.row,
            column: cell.column,
            row_span: cell.rowSpan,
            column_span: cell.columnSpan,
          },
          text: candidate.text,
        });
      }
    }
  }
  return segments;
}

function adaptNativeSegmentsToAssembler(
  segments: readonly NativeExtractedSegment[],
): SegmentTopologyHandoff {
  return {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source: "trusted_pdf_text_layer",
    segments: segments.map((segment) => ({
      segment_id: segment.segmentId,
      topology: segment.topology,
      text: segment.text,
    })),
  };
}

export function buildNativePdfOracle(
  items: readonly PdfTextGeometryItem[],
): NativePdfOracle | null {
  const candidates = items
    .map(candidateFromItem)
    .filter((candidate): candidate is NativeTextCandidate => candidate !== null);
  if (candidates.length === 0) return null;
  const objects = findNativePdfTextObjects(candidates);
  const segments = extractNativePdfTextSegments(candidates, objects);
  const assemblerHandoff = adaptNativeSegmentsToAssembler(segments);
  return {
    schema: PDF_NATIVE_ORACLE_SCHEMA,
    source: "trusted_pdf_text_layer",
    boundaryBasis: "pdfjs_text_item_bbox",
    nativeFindObject: { objects },
    nativeSegmentExtraction: { segments },
    assemblerHandoff,
    assembled: assembleSegmentTopologyHandoff(assemblerHandoff),
  };
}

export function serializeNativePdfOracle(oracle: NativePdfOracle): string {
  return JSON.stringify(oracle);
}

export function deserializeNativePdfOracle(value: string): NativePdfOracle {
  const oracle = JSON.parse(value) as NativePdfOracle;
  if (
    oracle.schema !== PDF_NATIVE_ORACLE_SCHEMA ||
    !Array.isArray(oracle.nativeFindObject?.objects) ||
    !Array.isArray(oracle.nativeSegmentExtraction?.segments) ||
    !Array.isArray(oracle.assemblerHandoff?.segments) ||
    !Array.isArray(oracle.assembled?.objects)
  ) {
    throw new Error("Unsupported native PDF oracle artifact");
  }
  return oracle;
}
