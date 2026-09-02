import {
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

export interface NativePdfTopologyAssembler {
  buildNativePdfParts(
    items: readonly PdfTextGeometryItem[],
  ): { objects: NativeTextObject[]; segments: NativeExtractedSegment[] } | null;
  assembleTopology(handoff: SegmentTopologyHandoff): StructuralRenderArtifact;
}

function adaptNativeSegmentsToAssembler(
  objects: readonly NativeTextObject[],
  segments: readonly NativeExtractedSegment[],
): SegmentTopologyHandoff {
  return {
    schema: SEGMENT_TOPOLOGY_HANDOFF_SCHEMA,
    source: "trusted_pdf_text_layer",
    object_layouts: objects.map((object) => ({
      object_id: object.objectId,
      object_kind: object.kind,
      logical_row_count: Math.max(
        1,
        ...object.cells.map((cell) => cell.row + cell.rowSpan),
      ),
      logical_column_count: Math.max(
        1,
        ...object.cells.map((cell) => cell.column + cell.columnSpan),
      ),
    })),
    segments: segments.map((segment) => ({
      segment_id: segment.segmentId,
      topology: segment.topology,
      text: segment.text,
    })),
  };
}

export function buildNativePdfOracle(
  items: readonly PdfTextGeometryItem[],
  assembler: NativePdfTopologyAssembler,
): NativePdfOracle | null {
  const parts = assembler.buildNativePdfParts(items);
  if (!parts) return null;
  const assemblerHandoff = adaptNativeSegmentsToAssembler(
    parts.objects,
    parts.segments,
  );
  return {
    schema: PDF_NATIVE_ORACLE_SCHEMA,
    source: "trusted_pdf_text_layer",
    boundaryBasis: "pdfjs_text_item_bbox",
    nativeFindObject: { objects: parts.objects },
    nativeSegmentExtraction: { segments: parts.segments },
    assemblerHandoff,
    assembled: assembler.assembleTopology(assemblerHandoff),
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
