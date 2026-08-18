export const SEGMENT_TOPOLOGY_HANDOFF_SCHEMA =
  "ittm.segment-topology-handoff/v1";
export const STRUCTURAL_RENDER_ARTIFACT_SCHEMA =
  "ittm.structural-render-artifact/v1";

export type StructuralObjectKind = "paragraph" | "table" | "small_table";

export interface SegmentTopology {
  object_id: string;
  object_kind: StructuralObjectKind;
  row: number;
  column: number;
  row_span: number;
  column_span: number;
}

export interface AssemblerSegment {
  segment_id: string;
  source_segment_ids?: string[];
  evidence?: {
    ocr_job_refs: number[];
  };
  topology: SegmentTopology;
  text: string;
}

export interface SegmentObjectLayout {
  object_id: string;
  object_kind: StructuralObjectKind;
  logical_row_count: number;
  logical_column_count: number;
}

export interface SegmentTopologyHandoff {
  schema: typeof SEGMENT_TOPOLOGY_HANDOFF_SCHEMA;
  source: "trusted_pdf_text_layer" | "raster_geometry";
  object_layouts?: SegmentObjectLayout[];
  evidence_index?: {
    ocr_job_ids: string[];
  };
  segments: AssemblerSegment[];
}

export interface StructuralCell {
  row: number;
  column: number;
  row_span: number;
  column_span: number;
  segment_ids: string[];
  text: string;
}

export interface StructuralObject {
  object_id: string;
  kind: StructuralObjectKind;
  logical_row_count: number;
  logical_column_count: number;
  cells: StructuralCell[];
  markdown: string;
}

export interface StructuralRenderArtifact {
  schema: typeof STRUCTURAL_RENDER_ARTIFACT_SCHEMA;
  objects: StructuralObject[];
  markdown: string;
}

function formatMarkdownTableCell(value: string): string {
  const ratio = value.match(/^\s*(\d+)\s*\/\s*(\d+)\s*$/);
  return ratio ? `${ratio[1]} / ${ratio[2]}` : value;
}

function assertHandoff(handoff: SegmentTopologyHandoff): void {
  if (handoff.schema !== SEGMENT_TOPOLOGY_HANDOFF_SCHEMA) {
    throw new Error("Unsupported segment topology handoff");
  }
  const layouts = new Map<string, SegmentObjectLayout>();
  for (const layout of handoff.object_layouts ?? []) {
    if (!layout.object_id || layouts.has(layout.object_id)) {
      throw new Error(`Invalid or duplicate object layout: ${layout.object_id}`);
    }
    if (
      !Number.isInteger(layout.logical_row_count) ||
      !Number.isInteger(layout.logical_column_count) ||
      layout.logical_row_count < 0 ||
      layout.logical_column_count < 0
    ) {
      throw new Error(`Invalid logical shape for object ${layout.object_id}`);
    }
    layouts.set(layout.object_id, layout);
  }
  const indexedJobIds = handoff.evidence_index?.ocr_job_ids;
  if (
    indexedJobIds &&
    (indexedJobIds.some((jobId) => !jobId) ||
      new Set(indexedJobIds).size !== indexedJobIds.length)
  ) {
    throw new Error("Invalid OCR job evidence index");
  }
  const segmentIds = new Set<string>();
  for (const segment of handoff.segments) {
    if (!segment.segment_id || segmentIds.has(segment.segment_id)) {
      throw new Error(`Invalid or duplicate segment_id: ${segment.segment_id}`);
    }
    segmentIds.add(segment.segment_id);
    const topology = segment.topology;
    if (
      !topology.object_id ||
      !Number.isInteger(topology.row) ||
      !Number.isInteger(topology.column) ||
      !Number.isInteger(topology.row_span) ||
      !Number.isInteger(topology.column_span) ||
      topology.row < 0 ||
      topology.column < 0 ||
      topology.row_span < 1 ||
      topology.column_span < 1
    ) {
      throw new Error(`Invalid topology for segment ${segment.segment_id}`);
    }
    const layout = layouts.get(topology.object_id);
    if (
      layout &&
      (layout.object_kind !== topology.object_kind ||
        topology.row + topology.row_span > layout.logical_row_count ||
        topology.column + topology.column_span >
          layout.logical_column_count)
    ) {
      throw new Error(
        `Segment ${segment.segment_id} exceeds its logical object layout`,
      );
    }
    if (
      segment.source_segment_ids?.some((sourceId) => !sourceId) ||
      segment.evidence?.ocr_job_refs.some(
        (jobRef) =>
          !Number.isInteger(jobRef) ||
          jobRef < 0 ||
          !indexedJobIds ||
          jobRef >= indexedJobIds.length,
      )
    ) {
      throw new Error(`Invalid evidence for segment ${segment.segment_id}`);
    }
  }
}

function markdownForObject(
  kind: StructuralObjectKind,
  cells: readonly StructuralCell[],
  logicalRowCount?: number,
  logicalColumnCount?: number,
): string {
  const inferredRowCount = Math.max(
    0,
    ...cells.map((cell) => cell.row + cell.row_span - 1),
  ) + (cells.length ? 1 : 0);
  const inferredColumnCount = Math.max(
    0,
    ...cells.map((cell) => cell.column + cell.column_span - 1),
  ) + (cells.length ? 1 : 0);
  const rowCount = logicalRowCount ?? inferredRowCount;
  const columnCount = logicalColumnCount ?? inferredColumnCount;
  const cellsByCoordinate = new Map(
    cells.map((cell) => [`${cell.row}:${cell.column}`, cell]),
  );
  const rows = Array.from({ length: rowCount }, (_, row) =>
    Array.from({ length: columnCount }, (_, column) =>
      cellsByCoordinate.get(`${row}:${column}`)?.text ?? "",
    ),
  );
  if (kind === "table" || kind === "small_table") {
    if (rows.length === 0) return "";
    const renderRow = (row: readonly string[]) =>
      `| ${row
        .map((value) => formatMarkdownTableCell(value).replaceAll("|", "\\|"))
        .join(" | ")} |`;
    return [
      renderRow(rows[0]),
      renderRow(rows[0].map(() => "---")),
      ...rows.slice(1).map(renderRow),
    ].join("\n");
  }
  return rows
    .map((row) => row.filter(Boolean).join(" ").trim())
    .filter(Boolean)
    .join("\n");
}

function materializeObjectCells(
  kind: StructuralObjectKind,
  segments: readonly AssemblerSegment[],
): StructuralCell[] {
  const anchors = new Map<string, StructuralCell>();
  for (const segment of segments) {
    const topology = segment.topology;
    const coordinateKey = `${topology.row}:${topology.column}`;
    const existing = anchors.get(coordinateKey);
    if (existing) {
      if (
        existing.row_span !== topology.row_span ||
        existing.column_span !== topology.column_span
      ) {
        throw new Error(
          `Cell ${topology.object_id}:${coordinateKey} mixes incompatible spans`,
        );
      }
      existing.segment_ids.push(segment.segment_id);
      const text = segment.text.trim();
      if (text) existing.text = [existing.text, text].filter(Boolean).join(" ");
      continue;
    }
    anchors.set(coordinateKey, {
      row: topology.row,
      column: topology.column,
      row_span: topology.row_span,
      column_span: topology.column_span,
      segment_ids: [segment.segment_id],
      text: segment.text.trim(),
    });
  }

  const orderedAnchors = [...anchors.values()].sort(
    (left, right) =>
      left.row - right.row ||
      left.column - right.column ||
      left.segment_ids[0].localeCompare(right.segment_ids[0]),
  );
  if (kind !== "table" && kind !== "small_table") return orderedAnchors;

  // A sparse detector may preserve a real anchor while overestimating the
  // span of a neighbouring merged cell. Anchors are stronger evidence than
  // inferred spans, so clip spans before materialising placeholder cells.
  // No anchor text is dropped and every resulting span remains at least 1x1.
  for (const cell of orderedAnchors) {
    for (const other of orderedAnchors) {
      if (other === cell) continue;
      const coversOtherAnchor =
        other.row >= cell.row &&
        other.row < cell.row + cell.row_span &&
        other.column >= cell.column &&
        other.column < cell.column + cell.column_span;
      if (!coversOtherAnchor) continue;
      const rowLimit = other.row - cell.row;
      const columnLimit = other.column - cell.column;
      if (rowLimit <= 0 && columnLimit > 0) {
        cell.column_span = Math.min(cell.column_span, columnLimit);
      } else if (columnLimit <= 0 && rowLimit > 0) {
        cell.row_span = Math.min(cell.row_span, rowLimit);
      } else if (rowLimit > 0 && columnLimit > 0) {
        const rowClipArea = rowLimit * cell.column_span;
        const columnClipArea = cell.row_span * columnLimit;
        if (rowClipArea >= columnClipArea) {
          cell.row_span = Math.min(cell.row_span, rowLimit);
        } else {
          cell.column_span = Math.min(cell.column_span, columnLimit);
        }
      }
    }
  }

  const occupied = new Map<string, string>();
  for (const cell of orderedAnchors) {
    const anchorKey = `${cell.row}:${cell.column}`;
    for (;;) {
      let conflict: { row: number; column: number } | undefined;
      for (
        let row = cell.row;
        row < cell.row + cell.row_span && !conflict;
        row += 1
      ) {
        for (
          let column = cell.column;
          column < cell.column + cell.column_span;
          column += 1
        ) {
          const previous = occupied.get(`${row}:${column}`);
          if (previous && previous !== anchorKey) {
            conflict = { row, column };
            break;
          }
        }
      }
      if (!conflict) break;
      const rowLimit = conflict.row - cell.row;
      const columnLimit = conflict.column - cell.column;
      if (rowLimit <= 0 && columnLimit <= 0) {
        throw new Error(
          `Object anchor overlap at ${cell.row}:${cell.column}`,
        );
      }
      if (rowLimit <= 0) {
        cell.column_span = Math.max(1, columnLimit);
      } else if (columnLimit <= 0) {
        cell.row_span = Math.max(1, rowLimit);
      } else if (
        rowLimit * cell.column_span >=
        cell.row_span * columnLimit
      ) {
        cell.row_span = rowLimit;
      } else {
        cell.column_span = columnLimit;
      }
    }
    for (let row = cell.row; row < cell.row + cell.row_span; row += 1) {
      for (
        let column = cell.column;
        column < cell.column + cell.column_span;
        column += 1
      ) {
        occupied.set(`${row}:${column}`, anchorKey);
      }
    }
  }

  return orderedAnchors;
}

function attachLeadingParagraphHeaders(
  objects: readonly StructuralObject[],
): StructuralObject[] {
  const merged: StructuralObject[] = [];
  for (let index = 0; index < objects.length; index += 1) {
    const paragraph = objects[index];
    const table = objects[index + 1];
    if (
      paragraph.kind !== "paragraph" ||
      paragraph.cells.length !== 1 ||
      !table ||
      (table.kind !== "table" && table.kind !== "small_table")
    ) {
      merged.push(paragraph);
      continue;
    }
    const columnCount = table.logical_column_count;
    const headerValues = paragraph.cells[0].text
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (columnCount < 2 || headerValues.length !== columnCount) {
      merged.push(paragraph);
      continue;
    }
    const headerCells = headerValues.map<StructuralCell>((text, column) => ({
      row: 0,
      column,
      row_span: 1,
      column_span: 1,
      segment_ids: column === 0 ? [...paragraph.cells[0].segment_ids] : [],
      text,
    }));
    const cells = [
      ...headerCells,
      ...table.cells.map((cell) => ({ ...cell, row: cell.row + 1 })),
    ];
    merged.push({
      ...table,
      logical_row_count: table.logical_row_count + 1,
      cells,
      markdown: markdownForObject(
        table.kind,
        cells,
        table.logical_row_count + 1,
        table.logical_column_count,
      ),
    });
    index += 1;
  }
  return merged;
}

export function assembleSegmentTopologyHandoff(
  handoff: SegmentTopologyHandoff,
): StructuralRenderArtifact {
  assertHandoff(handoff);
  const layouts = new Map(
    (handoff.object_layouts ?? []).map((layout) => [layout.object_id, layout]),
  );
  const grouped = new Map<string, AssemblerSegment[]>();
  for (const layout of handoff.object_layouts ?? []) {
    grouped.set(layout.object_id, []);
  }
  for (const segment of handoff.segments) {
    const group = grouped.get(segment.topology.object_id) ?? [];
    group.push(segment);
    grouped.set(segment.topology.object_id, group);
  }

  const materializedObjects = [...grouped.entries()].map(
    ([objectId, segments]) => {
    segments.sort(
      (left, right) =>
        left.topology.row - right.topology.row ||
        left.topology.column - right.topology.column ||
        left.segment_id.localeCompare(right.segment_id),
    );
    const layout = layouts.get(objectId);
    const kind = layout?.object_kind ?? segments[0].topology.object_kind;
    if (segments.some((segment) => segment.topology.object_kind !== kind)) {
      throw new Error(`Object ${objectId} mixes structural kinds`);
    }
    const cells = materializeObjectCells(kind, segments);
    const logicalRowCount =
      layout?.logical_row_count ??
      Math.max(0, ...cells.map((cell) => cell.row + cell.row_span));
    const logicalColumnCount =
      layout?.logical_column_count ??
      Math.max(0, ...cells.map((cell) => cell.column + cell.column_span));
    return {
      object_id: objectId,
      kind,
      logical_row_count: logicalRowCount,
      logical_column_count: logicalColumnCount,
      cells,
      markdown: markdownForObject(
        kind,
        cells,
        logicalRowCount,
        logicalColumnCount,
      ),
    };
    },
  );
  const objects =
    handoff.source === "raster_geometry"
      ? attachLeadingParagraphHeaders(materializedObjects)
      : materializedObjects;

  return {
    schema: STRUCTURAL_RENDER_ARTIFACT_SCHEMA,
    objects,
    markdown: objects
      .map((object) => object.markdown)
      .filter(Boolean)
      .join("\n\n"),
  };
}

export function serializeSegmentTopologyHandoff(
  handoff: SegmentTopologyHandoff,
): string {
  assertHandoff(handoff);
  return JSON.stringify(handoff);
}

export function deserializeSegmentTopologyHandoff(
  value: string,
): SegmentTopologyHandoff {
  const handoff = JSON.parse(value) as SegmentTopologyHandoff;
  assertHandoff(handoff);
  return handoff;
}
