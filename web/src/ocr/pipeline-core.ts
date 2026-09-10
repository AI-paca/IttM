import {
  STRUCTURAL_RENDER_ARTIFACT_SCHEMA,
  type SegmentTopologyHandoff,
  type StructuralCell,
  type StructuralObject,
  type StructuralObjectKind,
  type StructuralRenderArtifact,
} from "./segment-assembler";
import type {
  NativeExtractedSegment,
  NativeTextObject,
  PdfTextGeometryItem,
} from "../lib/pdf-native-oracle";

export const PIPELINE_CORE_ABI_VERSION = 6;

export const PIPELINE_STAGES = [
  "align",
  "segment",
  "project_sparse",
  "recognize_segments",
  "select_language_candidate",
  "lexical_correction",
  "group_structures",
  "render_markdown",
] as const;

export type PipelineStage = (typeof PIPELINE_STAGES)[number];

export const SEPARATED_PIPELINE_STAGES = [
  "preprocess",
  "geometry",
  "topology",
  "find-object",
  "separate-block",
  "ocr-blocks",
  "get-segment",
  "generate-object",
] as const;

const SEPARATED_LANGUAGE_PROFILES = [
  "rus+eng",
  "rus",
  "eng",
  "chi_sim",
  "ell",
  "equ",
] as const;
const SEPARATED_TRANSFORMS = ["raw", "gamma-dark"] as const;

export interface SeparatedOcrJob {
  index: number;
  bbox: readonly [number, number, number, number];
  objectId: number;
  row: number;
  column: number;
  rowSpan: number;
  columnSpan: number;
  recognitionMode: number;
  objectKind: number;
  languages: string;
  transform: string;
  depth: number;
  logicalRowCount: number;
  logicalColumnCount: number;
  grammarMilli: number;
  superseded: boolean;
}

export interface SeparatedObject {
  index: number;
  bbox: readonly [number, number, number, number];
  objectKind: number;
  segmentIndexes: readonly number[];
  readingIndex: number;
  rowStart: number;
  rowStop: number;
  columnStart: number;
  columnStop: number;
}

export interface SeparatedBlock {
  index: number;
  bbox: readonly [number, number, number, number];
  objectId: number;
  segmentIndexes: readonly number[];
  dyadicMask: boolean;
  matrixWindow: readonly [number, number, number, number];
  logicalScopeShape: readonly [number, number];
}

export interface SeparatedOcrRaster {
  pixels: Uint8Array;
  width: number;
  height: number;
  stride: number;
  format: 3;
}

export interface SeparatedOcrWord {
  text: string;
  bbox: readonly [number, number, number, number];
  confidenceMilli: number;
}

export interface PipelineCapabilities {
  trustedText?: boolean;
  providesLayout?: boolean;
  providesMarkdown?: boolean;
  needsLanguageRetry?: boolean;
}

interface PipelineCoreExports extends WebAssembly.Exports {
  ittm_pipeline_abi_version(): number;
  ittm_pipeline_route_id(): number;
  ittm_pipeline_recipe_mask(capabilityBits: number): number;
  ittm_sparse_add_signal(code: number, signal: number): number;
  ittm_is_isolated_heading(
    runRows: number,
    contentChars: number,
    maxLineChars: number,
    boundaryBits: number,
  ): number;
  ittm_span_evidence_score(
    ocrConfidenceMilli: number,
    scriptConsistencyMilli: number,
    contextConsistencyMilli: number,
    sourceAgreement: number,
    contradictions: number,
  ): number;
  ittm_should_replace_primary(
    primaryChars: number,
    fallbackChars: number,
    primaryTokens: number,
    retainedPrimaryTokens: number,
  ): number;
  ittm_should_drop_text_block(
    candidateChars: number,
    existingChars: number,
    sharedTokens: number,
    candidateTokens: number,
    existingTokens: number,
    similarityMilli: number,
  ): number;
  memory?: WebAssembly.Memory;
  ittm_alloc?(length: number): number;
  ittm_dealloc?(pointer: number, capacity: number): void;
  ittm_assembler_begin?(source: number): number;
  ittm_assembler_add_layout?(
    handle: number,
    objectId: number,
    objectIdLength: number,
    objectKind: number,
    logicalRowCount: number,
    logicalColumnCount: number,
  ): number;
  ittm_assembler_add_segment?(
    handle: number,
    segmentId: number,
    segmentIdLength: number,
    objectId: number,
    objectIdLength: number,
    objectKind: number,
    row: number,
    column: number,
    rowSpan: number,
    columnSpan: number,
    text: number,
    textLength: number,
  ): number;
  ittm_assembler_render_length?(handle: number): number;
  ittm_assembler_render_copy?(
    handle: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_assembler_object_count?(handle: number): number;
  ittm_assembler_object_field?(
    handle: number,
    object: number,
    field: number,
  ): number;
  ittm_assembler_object_id_length?(handle: number, object: number): number;
  ittm_assembler_object_id_copy?(
    handle: number,
    object: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_assembler_object_markdown_length?(
    handle: number,
    object: number,
  ): number;
  ittm_assembler_object_markdown_copy?(
    handle: number,
    object: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_assembler_cell_field?(
    handle: number,
    object: number,
    cell: number,
    field: number,
  ): number;
  ittm_assembler_cell_text_length?(
    handle: number,
    object: number,
    cell: number,
  ): number;
  ittm_assembler_cell_text_copy?(
    handle: number,
    object: number,
    cell: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_assembler_cell_segment_id_length?(
    handle: number,
    object: number,
    cell: number,
    segment: number,
  ): number;
  ittm_assembler_cell_segment_id_copy?(
    handle: number,
    object: number,
    cell: number,
    segment: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_assembler_drop?(handle: number): number;
  ittm_pdf_native_begin?(): number;
  ittm_pdf_native_add_item?(
    handle: number,
    text: number,
    textLength: number,
    width: number,
    height: number,
    hasHeight: number,
    transform0: number,
    transform1: number,
    transform2: number,
    transform3: number,
    transform4: number,
    transform5: number,
  ): number;
  ittm_pdf_native_build?(handle: number): number;
  ittm_pdf_native_render_length?(handle: number): number;
  ittm_pdf_native_render_copy?(
    handle: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_pdf_native_drop?(handle: number): number;
  ittm_separated_begin?(
    pointer: number,
    byteLength: number,
    width: number,
    height: number,
    stride: number,
    format: number,
  ): number;
  ittm_separated_plan_begin?(
    pointer: number,
    byteLength: number,
    width: number,
    height: number,
    stride: number,
    format: number,
  ): number;
  ittm_separated_start_ocr?(handle: number): number;
  ittm_separated_run_get_segment?(handle: number): number;
  ittm_separated_object_count?(handle: number): number;
  ittm_separated_object_field?(
    handle: number,
    index: number,
    field: number,
  ): number;
  ittm_separated_object_segment?(
    handle: number,
    objectIndex: number,
    segmentIndex: number,
  ): number;
  ittm_separated_block_count?(handle: number): number;
  ittm_separated_block_field?(
    handle: number,
    index: number,
    field: number,
  ): number;
  ittm_separated_block_segment?(
    handle: number,
    blockIndex: number,
    segmentIndex: number,
  ): number;
  ittm_separated_block_raster_field?(
    handle: number,
    blockIndex: number,
    field: number,
  ): number;
  ittm_separated_block_raster_length?(
    handle: number,
    blockIndex: number,
  ): number;
  ittm_separated_block_raster_copy?(
    handle: number,
    blockIndex: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_separated_job_count?(handle: number): number;
  ittm_separated_job_field?(
    handle: number,
    index: number,
    field: number,
  ): number;
  ittm_separated_job_raster_field?(
    handle: number,
    index: number,
    field: number,
  ): number;
  ittm_separated_job_raster_length?(handle: number, index: number): number;
  ittm_separated_job_raster_copy?(
    handle: number,
    index: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_separated_set_ocr?(
    handle: number,
    index: number,
    pointer: number,
    byteLength: number,
    confidenceMilli: number,
  ): number;
  ittm_separated_add_ocr_word?(
    handle: number,
    index: number,
    pointer: number,
    byteLength: number,
    left: number,
    top: number,
    right: number,
    bottom: number,
    confidenceMilli: number,
  ): number;
  ittm_separated_add_ocr_word_ppm?(
    handle: number,
    index: number,
    pointer: number,
    byteLength: number,
    left: number,
    top: number,
    right: number,
    bottom: number,
    confidencePpm: number,
  ): number;
  ittm_separated_render_length?(handle: number): number;
  ittm_separated_render_copy?(
    handle: number,
    pointer: number,
    capacity: number,
  ): number;
  ittm_separated_stage_mask?(handle: number): number;
  ittm_separated_drop?(handle: number): number;
}

type SeparatedExports = Required<
  Pick<
    PipelineCoreExports,
    | "memory"
    | "ittm_alloc"
    | "ittm_dealloc"
    | "ittm_separated_begin"
    | "ittm_separated_plan_begin"
    | "ittm_separated_start_ocr"
    | "ittm_separated_run_get_segment"
    | "ittm_separated_object_count"
    | "ittm_separated_object_field"
    | "ittm_separated_object_segment"
    | "ittm_separated_block_count"
    | "ittm_separated_block_field"
    | "ittm_separated_block_segment"
    | "ittm_separated_block_raster_field"
    | "ittm_separated_block_raster_length"
    | "ittm_separated_block_raster_copy"
    | "ittm_separated_job_count"
    | "ittm_separated_job_field"
    | "ittm_separated_job_raster_field"
    | "ittm_separated_job_raster_length"
    | "ittm_separated_job_raster_copy"
    | "ittm_separated_set_ocr"
    | "ittm_separated_add_ocr_word"
    | "ittm_separated_add_ocr_word_ppm"
    | "ittm_separated_render_length"
    | "ittm_separated_render_copy"
    | "ittm_separated_stage_mask"
    | "ittm_separated_drop"
  >
>;

type AssemblerExports = Required<
  Pick<
    PipelineCoreExports,
    | "memory"
    | "ittm_alloc"
    | "ittm_dealloc"
    | "ittm_assembler_begin"
    | "ittm_assembler_add_layout"
    | "ittm_assembler_add_segment"
    | "ittm_assembler_render_length"
    | "ittm_assembler_render_copy"
    | "ittm_assembler_object_count"
    | "ittm_assembler_object_field"
    | "ittm_assembler_object_id_length"
    | "ittm_assembler_object_id_copy"
    | "ittm_assembler_object_markdown_length"
    | "ittm_assembler_object_markdown_copy"
    | "ittm_assembler_cell_field"
    | "ittm_assembler_cell_text_length"
    | "ittm_assembler_cell_text_copy"
    | "ittm_assembler_cell_segment_id_length"
    | "ittm_assembler_cell_segment_id_copy"
    | "ittm_assembler_drop"
  >
>;

type NativePdfExports = Required<
  Pick<
    PipelineCoreExports,
    | "memory"
    | "ittm_alloc"
    | "ittm_dealloc"
    | "ittm_pdf_native_begin"
    | "ittm_pdf_native_add_item"
    | "ittm_pdf_native_build"
    | "ittm_pdf_native_render_length"
    | "ittm_pdf_native_render_copy"
    | "ittm_pdf_native_drop"
  >
>;

function assertNonNegativeIntegers(values: readonly number[], label: string) {
  if (values.some((value) => !Number.isInteger(value) || value < 0)) {
    throw new Error(`${label} must be non-negative integers`);
  }
}

export function capabilityBits(capabilities: PipelineCapabilities): number {
  return (
    Number(Boolean(capabilities.trustedText)) |
    (Number(Boolean(capabilities.providesLayout)) << 1) |
    (Number(Boolean(capabilities.providesMarkdown)) << 2) |
    (Number(capabilities.needsLanguageRetry !== false) << 3)
  );
}

export class BrowserPipelineCore {
  constructor(private readonly exports: PipelineCoreExports) {
    const version = exports.ittm_pipeline_abi_version();
    if (version !== PIPELINE_CORE_ABI_VERSION) {
      throw new Error(
        `Unsupported pipeline core ABI ${version}; expected ${PIPELINE_CORE_ABI_VERSION}`,
      );
    }
    if (typeof exports.ittm_pipeline_route_id !== "function") {
      throw new Error(
        `Pipeline core ABI ${PIPELINE_CORE_ABI_VERSION} misses ittm_pipeline_route_id`,
      );
    }
  }

  routeId(): number {
    return this.exports.ittm_pipeline_route_id() >>> 0;
  }

  recipe(capabilities: PipelineCapabilities): ReadonlySet<PipelineStage> {
    const mask = this.exports.ittm_pipeline_recipe_mask(
      capabilityBits(capabilities),
    );
    return new Set(
      PIPELINE_STAGES.filter((_stage, index) => mask & (1 << index)),
    );
  }

  beginSeparated(options: {
    pixels: Uint8Array | Uint8ClampedArray;
    width: number;
    height: number;
    stride?: number;
    format?: 1 | 3 | 4;
  }): BrowserSeparatedSession {
    const separated = separatedExports(this.exports);
    const format = options.format ?? 4;
    const stride = options.stride ?? options.width * format;
    const expectedLength = stride * options.height;
    if (
      !Number.isInteger(options.width) ||
      !Number.isInteger(options.height) ||
      options.width <= 0 ||
      options.height <= 0 ||
      options.pixels.byteLength !== expectedLength
    ) {
      throw new Error("Invalid separated pipeline raster plane");
    }
    const pointer = copyIntoWasm(separated, options.pixels);
    let handle: number;
    try {
      handle = separated.ittm_separated_plan_begin(
        pointer,
        options.pixels.byteLength,
        options.width,
        options.height,
        stride,
        format,
      );
    } finally {
      separated.ittm_dealloc(pointer, options.pixels.byteLength);
    }
    if (!handle)
      throw new Error("Separated pipeline rejected the raster plane");
    return new BrowserSeparatedSession(separated, handle);
  }

  assembleTopology(handoff: SegmentTopologyHandoff): StructuralRenderArtifact {
    const assembler = assemblerExports(this.exports);
    const source = handoff.source === "trusted_pdf_text_layer" ? 0 : 1;
    const kindCode = (kind: StructuralObjectKind) =>
      ({ paragraph: 0, table: 1, small_table: 2 })[kind];
    const kindName = (kind: number): StructuralObjectKind => {
      const value = (["paragraph", "table", "small_table"] as const)[kind];
      if (!value)
        throw new Error(`Rust assembler returned object kind ${kind}`);
      return value;
    };
    const encoder = new TextEncoder();
    const handle = assembler.ittm_assembler_begin(source);
    if (!handle) throw new Error("Rust topology assembler rejected its source");
    let result: StructuralRenderArtifact | undefined;
    let operationError: unknown;
    try {
      for (const layout of handoff.object_layouts ?? []) {
        const objectId = encoder.encode(layout.object_id);
        const objectPointer = copyIntoWasm(assembler, objectId);
        try {
          const status = assembler.ittm_assembler_add_layout(
            handle,
            objectPointer,
            objectId.byteLength,
            kindCode(layout.object_kind),
            layout.logical_row_count,
            layout.logical_column_count,
          );
          if (status !== 0) {
            throw new Error(
              `Rust topology layout handoff failed with status ${status}`,
            );
          }
        } finally {
          if (objectPointer)
            assembler.ittm_dealloc(objectPointer, objectId.byteLength);
        }
      }
      for (const segment of handoff.segments) {
        const segmentId = encoder.encode(segment.segment_id);
        const objectId = encoder.encode(segment.topology.object_id);
        const text = encoder.encode(segment.text);
        const segmentPointer = copyIntoWasm(assembler, segmentId);
        const objectPointer = copyIntoWasm(assembler, objectId);
        const textPointer = copyIntoWasm(assembler, text);
        try {
          const status = assembler.ittm_assembler_add_segment(
            handle,
            segmentPointer,
            segmentId.byteLength,
            objectPointer,
            objectId.byteLength,
            kindCode(segment.topology.object_kind),
            segment.topology.row,
            segment.topology.column,
            segment.topology.row_span,
            segment.topology.column_span,
            textPointer,
            text.byteLength,
          );
          if (status !== 0) {
            throw new Error(
              `Rust topology segment handoff failed with status ${status}`,
            );
          }
        } finally {
          if (segmentPointer)
            assembler.ittm_dealloc(segmentPointer, segmentId.byteLength);
          if (objectPointer)
            assembler.ittm_dealloc(objectPointer, objectId.byteLength);
          if (textPointer) assembler.ittm_dealloc(textPointer, text.byteLength);
        }
      }

      const markdownLength = assembler.ittm_assembler_render_length(handle);
      const markdown = readAssemblerText(
        assembler,
        markdownLength,
        (pointer, capacity) =>
          assembler.ittm_assembler_render_copy(handle, pointer, capacity),
      );
      const objects: StructuralObject[] = [];
      const objectCount = assembler.ittm_assembler_object_count(handle);
      for (let objectIndex = 0; objectIndex < objectCount; objectIndex += 1) {
        const field = (index: number) => {
          const value = assembler.ittm_assembler_object_field(
            handle,
            objectIndex,
            index,
          );
          if (value < 0)
            throw new Error(
              `Rust topology object field failed with status ${value}`,
            );
          return value;
        };
        const objectId = readAssemblerText(
          assembler,
          assembler.ittm_assembler_object_id_length(handle, objectIndex),
          (pointer, capacity) =>
            assembler.ittm_assembler_object_id_copy(
              handle,
              objectIndex,
              pointer,
              capacity,
            ),
        );
        const objectMarkdown = readAssemblerText(
          assembler,
          assembler.ittm_assembler_object_markdown_length(handle, objectIndex),
          (pointer, capacity) =>
            assembler.ittm_assembler_object_markdown_copy(
              handle,
              objectIndex,
              pointer,
              capacity,
            ),
        );
        const cells: StructuralCell[] = [];
        const cellCount = field(3);
        for (let cellIndex = 0; cellIndex < cellCount; cellIndex += 1) {
          const cellField = (index: number) => {
            const value = assembler.ittm_assembler_cell_field(
              handle,
              objectIndex,
              cellIndex,
              index,
            );
            if (value < 0)
              throw new Error(
                `Rust topology cell field failed with status ${value}`,
              );
            return value;
          };
          const segmentIds = Array.from(
            { length: cellField(4) },
            (_unused, segmentIndex) =>
              readAssemblerText(
                assembler,
                assembler.ittm_assembler_cell_segment_id_length(
                  handle,
                  objectIndex,
                  cellIndex,
                  segmentIndex,
                ),
                (pointer, capacity) =>
                  assembler.ittm_assembler_cell_segment_id_copy(
                    handle,
                    objectIndex,
                    cellIndex,
                    segmentIndex,
                    pointer,
                    capacity,
                  ),
              ),
          );
          cells.push({
            row: cellField(0),
            column: cellField(1),
            row_span: cellField(2),
            column_span: cellField(3),
            segment_ids: segmentIds,
            text: readAssemblerText(
              assembler,
              assembler.ittm_assembler_cell_text_length(
                handle,
                objectIndex,
                cellIndex,
              ),
              (pointer, capacity) =>
                assembler.ittm_assembler_cell_text_copy(
                  handle,
                  objectIndex,
                  cellIndex,
                  pointer,
                  capacity,
                ),
            ),
          });
        }
        objects.push({
          object_id: objectId,
          kind: kindName(field(0)),
          logical_row_count: field(1),
          logical_column_count: field(2),
          cells,
          markdown: objectMarkdown,
        });
      }
      result = {
        schema: STRUCTURAL_RENDER_ARTIFACT_SCHEMA,
        objects,
        markdown,
      };
    } catch (error) {
      operationError = error;
    }
    const dropStatus = assembler.ittm_assembler_drop(handle);
    if (operationError !== undefined) throw operationError;
    if (dropStatus !== 0) {
      throw new Error(`Unknown Rust topology assembler handle: ${handle}`);
    }
    if (!result)
      throw new Error("Rust topology assembler returned no artifact");
    return result;
  }

  buildNativePdfParts(items: readonly PdfTextGeometryItem[]): {
    objects: NativeTextObject[];
    segments: NativeExtractedSegment[];
  } | null {
    const nativePdf = nativePdfExports(this.exports);
    const encoder = new TextEncoder();
    const handle = nativePdf.ittm_pdf_native_begin();
    if (!handle)
      throw new Error("Rust native PDF route rejected a new session");
    let result:
      | { objects: NativeTextObject[]; segments: NativeExtractedSegment[] }
      | null
      | undefined;
    let operationError: unknown;
    try {
      for (const item of items) {
        const text = encoder.encode(item.str ?? "");
        const textPointer = copyIntoWasm(nativePdf, text);
        const transform = Array.from({ length: 6 }, (_unused, index) =>
          Number(
            item.transform?.[index] ?? (index === 0 || index === 3 ? 1 : 0),
          ),
        );
        const hasHeight =
          typeof item.height === "number" && Number.isFinite(item.height);
        try {
          const status = nativePdf.ittm_pdf_native_add_item(
            handle,
            textPointer,
            text.byteLength,
            Number(item.width ?? 0),
            hasHeight ? Number(item.height) : 0,
            Number(hasHeight),
            transform[0],
            transform[1],
            transform[2],
            transform[3],
            transform[4],
            transform[5],
          );
          if (status !== 0) {
            throw new Error(
              `Rust native PDF item handoff failed with status ${status}`,
            );
          }
        } finally {
          if (textPointer) nativePdf.ittm_dealloc(textPointer, text.byteLength);
        }
      }
      const buildStatus = nativePdf.ittm_pdf_native_build(handle);
      if (buildStatus !== 0) {
        throw new Error(
          `Rust native PDF build failed with status ${buildStatus}`,
        );
      }
      const length = nativePdf.ittm_pdf_native_render_length(handle);
      const rendered = readNativePdfText(
        nativePdf,
        length,
        (pointer, capacity) =>
          nativePdf.ittm_pdf_native_render_copy(handle, pointer, capacity),
      );
      const parts = JSON.parse(rendered) as {
        objects?: NativeTextObject[];
        segments?: NativeExtractedSegment[];
      } | null;
      if (parts === null) {
        result = null;
      } else if (
        !Array.isArray(parts.objects) ||
        !Array.isArray(parts.segments)
      ) {
        throw new Error("Rust native PDF route returned an invalid artifact");
      } else {
        result = { objects: parts.objects, segments: parts.segments };
      }
    } catch (error) {
      operationError = error;
    }
    const dropStatus = nativePdf.ittm_pdf_native_drop(handle);
    if (operationError !== undefined) throw operationError;
    if (dropStatus !== 0) {
      throw new Error(`Unknown Rust native PDF handle: ${handle}`);
    }
    if (result === undefined)
      throw new Error("Rust native PDF route returned no artifact");
    return result;
  }

  addSparseSignal(code: number, signal: number): number {
    const result = this.exports.ittm_sparse_add_signal(code, signal);
    if (result === -2) throw new Error(`Unknown sparse signal: ${signal}`);
    if (result === -1) throw new Error(`Unknown sparse code: ${code}`);
    return result;
  }

  isIsolatedHeading(options: {
    runRows: number;
    contentChars: number;
    maxLineChars: number;
    boundedAbove: boolean;
    boundedBelow: boolean;
  }): boolean {
    const boundaryBits =
      Number(options.boundedAbove) | (Number(options.boundedBelow) << 1);
    return Boolean(
      this.exports.ittm_is_isolated_heading(
        options.runRows,
        options.contentChars,
        options.maxLineChars,
        boundaryBits,
      ),
    );
  }

  spanEvidenceScore(options: {
    ocrConfidenceMilli: number;
    scriptConsistencyMilli: number;
    contextConsistencyMilli: number;
    sourceAgreement: number;
    contradictions?: number;
  }): number {
    const values = [
      options.ocrConfidenceMilli,
      options.scriptConsistencyMilli,
      options.contextConsistencyMilli,
      options.sourceAgreement,
      options.contradictions ?? 0,
    ];
    assertNonNegativeIntegers(values, "Span evidence values");
    return this.exports.ittm_span_evidence_score(
      options.ocrConfidenceMilli,
      options.scriptConsistencyMilli,
      options.contextConsistencyMilli,
      options.sourceAgreement,
      options.contradictions ?? 0,
    );
  }

  shouldReplacePrimary(options: {
    primaryChars: number;
    fallbackChars: number;
    primaryTokens: number;
    retainedPrimaryTokens: number;
  }): boolean {
    const values = [
      options.primaryChars,
      options.fallbackChars,
      options.primaryTokens,
      options.retainedPrimaryTokens,
    ];
    assertNonNegativeIntegers(values, "Primary replacement values");
    return Boolean(
      this.exports.ittm_should_replace_primary(
        options.primaryChars,
        options.fallbackChars,
        options.primaryTokens,
        options.retainedPrimaryTokens,
      ),
    );
  }

  shouldDropTextBlock(options: {
    candidateChars: number;
    existingChars: number;
    sharedTokens: number;
    candidateTokens: number;
    existingTokens: number;
    similarityMilli: number;
  }): boolean {
    const values = [
      options.candidateChars,
      options.existingChars,
      options.sharedTokens,
      options.candidateTokens,
      options.existingTokens,
      options.similarityMilli,
    ];
    assertNonNegativeIntegers(values, "Text block deduplication values");
    return Boolean(
      this.exports.ittm_should_drop_text_block(
        options.candidateChars,
        options.existingChars,
        options.sharedTokens,
        options.candidateTokens,
        options.existingTokens,
        options.similarityMilli,
      ),
    );
  }
}

function separatedExports(exports: PipelineCoreExports): SeparatedExports {
  const required = [
    "memory",
    "ittm_alloc",
    "ittm_dealloc",
    "ittm_separated_begin",
    "ittm_separated_plan_begin",
    "ittm_separated_start_ocr",
    "ittm_separated_run_get_segment",
    "ittm_separated_job_count",
    "ittm_separated_job_field",
    "ittm_separated_job_raster_field",
    "ittm_separated_job_raster_length",
    "ittm_separated_job_raster_copy",
    "ittm_separated_set_ocr",
    "ittm_separated_add_ocr_word",
    "ittm_separated_render_length",
    "ittm_separated_render_copy",
    "ittm_separated_stage_mask",
    "ittm_separated_drop",
  ] as const;
  for (const name of required) {
    if (!exports[name]) {
      throw new Error(
        `Pipeline core ABI ${PIPELINE_CORE_ABI_VERSION} misses ${name}`,
      );
    }
  }
  return exports as SeparatedExports;
}

function assemblerExports(exports: PipelineCoreExports): AssemblerExports {
  const required = [
    "memory",
    "ittm_alloc",
    "ittm_dealloc",
    "ittm_assembler_begin",
    "ittm_assembler_add_layout",
    "ittm_assembler_add_segment",
    "ittm_assembler_render_length",
    "ittm_assembler_render_copy",
    "ittm_assembler_object_count",
    "ittm_assembler_object_field",
    "ittm_assembler_object_id_length",
    "ittm_assembler_object_id_copy",
    "ittm_assembler_object_markdown_length",
    "ittm_assembler_object_markdown_copy",
    "ittm_assembler_cell_field",
    "ittm_assembler_cell_text_length",
    "ittm_assembler_cell_text_copy",
    "ittm_assembler_cell_segment_id_length",
    "ittm_assembler_cell_segment_id_copy",
    "ittm_assembler_drop",
  ] as const;
  for (const name of required) {
    if (!exports[name]) {
      throw new Error(
        `Pipeline core ABI ${PIPELINE_CORE_ABI_VERSION} misses ${name}`,
      );
    }
  }
  return exports as AssemblerExports;
}

function nativePdfExports(exports: PipelineCoreExports): NativePdfExports {
  const required = [
    "memory",
    "ittm_alloc",
    "ittm_dealloc",
    "ittm_pdf_native_begin",
    "ittm_pdf_native_add_item",
    "ittm_pdf_native_build",
    "ittm_pdf_native_render_length",
    "ittm_pdf_native_render_copy",
    "ittm_pdf_native_drop",
  ] as const;
  for (const name of required) {
    if (!exports[name]) {
      throw new Error(
        `Pipeline core ABI ${PIPELINE_CORE_ABI_VERSION} misses ${name}`,
      );
    }
  }
  return exports as NativePdfExports;
}

function copyIntoWasm(
  exports: Pick<SeparatedExports, "memory" | "ittm_alloc">,
  bytes: Uint8Array | Uint8ClampedArray,
): number {
  if (bytes.byteLength === 0) return 0;
  const pointer = exports.ittm_alloc(bytes.byteLength) >>> 0;
  if (!pointer) throw new Error("Pipeline core could not allocate WASM memory");
  new Uint8Array(exports.memory.buffer, pointer, bytes.byteLength).set(bytes);
  return pointer;
}

function readAssemblerText(
  exports: AssemblerExports,
  length: number,
  copy: (pointer: number, capacity: number) => number,
): string {
  if (!length) return "";
  const pointer = exports.ittm_alloc(length) >>> 0;
  if (!pointer)
    throw new Error("Pipeline core could not allocate assembler output");
  try {
    const copied = copy(pointer, length);
    if (copied !== length) {
      throw new Error(
        `Rust topology assembler copied ${copied} bytes; expected ${length}`,
      );
    }
    return new TextDecoder().decode(
      new Uint8Array(exports.memory.buffer, pointer, length),
    );
  } finally {
    exports.ittm_dealloc(pointer, length);
  }
}

function readNativePdfText(
  exports: NativePdfExports,
  length: number,
  copy: (pointer: number, capacity: number) => number,
): string {
  if (!length) throw new Error("Rust native PDF route returned no artifact");
  const pointer = exports.ittm_alloc(length) >>> 0;
  if (!pointer)
    throw new Error("Pipeline core could not allocate native PDF output");
  try {
    const copied = copy(pointer, length);
    if (copied !== length) {
      throw new Error(
        `Rust native PDF route copied ${copied} bytes; expected ${length}`,
      );
    }
    return new TextDecoder().decode(
      new Uint8Array(exports.memory.buffer, pointer, length),
    );
  } finally {
    exports.ittm_dealloc(pointer, length);
  }
}

export class BrowserSeparatedSession {
  private closed = false;

  constructor(
    private readonly exports: SeparatedExports,
    private readonly handle: number,
  ) {}

  startOcr(): void {
    this.assertOpen();
    if (this.exports.ittm_separated_start_ocr(this.handle) !== 1) {
      throw new Error("Separated OCR stage could not start");
    }
  }

  runGetSegment(): void {
    this.assertOpen();
    if (this.exports.ittm_separated_run_get_segment(this.handle) !== 1) {
      throw new Error("Separated get-segment stage could not run");
    }
  }

  objects(): readonly SeparatedObject[] {
    this.assertOpen();
    return Array.from(
      { length: this.exports.ittm_separated_object_count(this.handle) },
      (_unused, index) => {
        const field = (fieldIndex: number) => {
          const value = this.exports.ittm_separated_object_field(
            this.handle,
            index,
            fieldIndex,
          );
          if (value < 0) {
            throw new Error(
              `Invalid separated object field: ${index}:${fieldIndex}`,
            );
          }
          return value;
        };
        const segmentCount = field(5);
        const segmentIndexes = Array.from(
          { length: segmentCount },
          (_value, segment) => {
            const value = this.exports.ittm_separated_object_segment(
              this.handle,
              index,
              segment,
            );
            if (value < 0)
              throw new Error(
                `Invalid separated object segment: ${index}:${segment}`,
              );
            return value;
          },
        );
        return {
          index,
          bbox: [field(0), field(1), field(2), field(3)] as const,
          objectKind: field(4),
          segmentIndexes,
          readingIndex: field(6),
          rowStart: field(7),
          rowStop: field(8),
          columnStart: field(9),
          columnStop: field(10),
        };
      },
    );
  }

  blocks(): readonly SeparatedBlock[] {
    this.assertOpen();
    return Array.from(
      { length: this.exports.ittm_separated_block_count(this.handle) },
      (_unused, index) => {
        const field = (fieldIndex: number) => {
          const value = this.exports.ittm_separated_block_field(
            this.handle,
            index,
            fieldIndex,
          );
          if (value < 0) {
            throw new Error(
              `Invalid separated block field: ${index}:${fieldIndex}`,
            );
          }
          return value;
        };
        const segmentCount = field(5);
        const segmentIndexes = Array.from(
          { length: segmentCount },
          (_value, segment) => {
            const value = this.exports.ittm_separated_block_segment(
              this.handle,
              index,
              segment,
            );
            if (value < 0)
              throw new Error(
                `Invalid separated block segment: ${index}:${segment}`,
              );
            return value;
          },
        );
        return {
          index,
          bbox: [field(0), field(1), field(2), field(3)] as const,
          objectId: field(4),
          segmentIndexes,
          dyadicMask: Boolean(field(6)),
          matrixWindow: [field(7), field(8), field(9), field(10)] as const,
          logicalScopeShape: [field(11), field(12)] as const,
        };
      },
    );
  }

  blockRaster(index: number): SeparatedOcrRaster {
    this.assertOpen();
    const field = (fieldIndex: number) => {
      const value = this.exports.ittm_separated_block_raster_field(
        this.handle,
        index,
        fieldIndex,
      );
      if (value <= 0) {
        throw new Error(
          `Invalid separated block raster field: ${index}:${fieldIndex}`,
        );
      }
      return value;
    };
    const width = field(0);
    const height = field(1);
    const stride = field(2);
    const format = field(3);
    if (format !== 3 || stride !== width * format) {
      throw new Error("Separated block raster is not packed RGB");
    }
    const length = this.exports.ittm_separated_block_raster_length(
      this.handle,
      index,
    );
    if (length !== stride * height) {
      throw new Error(
        "Separated block raster length disagrees with its dimensions",
      );
    }
    const pointer = this.exports.ittm_alloc(length) >>> 0;
    if (!pointer)
      throw new Error("Pipeline core could not allocate block raster buffer");
    try {
      const copied = this.exports.ittm_separated_block_raster_copy(
        this.handle,
        index,
        pointer,
        length,
      );
      if (copied !== length) {
        throw new Error(
          `Separated block raster copied ${copied} bytes; expected ${length}`,
        );
      }
      return {
        pixels: new Uint8Array(
          this.exports.memory.buffer,
          pointer,
          length,
        ).slice(),
        width,
        height,
        stride,
        format: 3,
      };
    } finally {
      this.exports.ittm_dealloc(pointer, length);
    }
  }

  jobCount(): number {
    this.assertOpen();
    return this.exports.ittm_separated_job_count(this.handle);
  }

  job(index: number): SeparatedOcrJob {
    this.assertOpen();
    const field = (index: number, fieldIndex: number) => {
      const value = this.exports.ittm_separated_job_field(
        this.handle,
        index,
        fieldIndex,
      );
      if (value < 0) {
        throw new Error(
          `Invalid separated OCR job field: ${index}:${fieldIndex}`,
        );
      }
      return value;
    };
    return {
      index,
      bbox: [
        field(index, 0),
        field(index, 1),
        field(index, 2),
        field(index, 3),
      ] as const,
      objectId: field(index, 4),
      row: field(index, 5),
      column: field(index, 6),
      rowSpan: field(index, 7),
      columnSpan: field(index, 8),
      recognitionMode: field(index, 9),
      objectKind: field(index, 10),
      languages: SEPARATED_LANGUAGE_PROFILES[field(index, 11)] ?? "rus+eng",
      transform: SEPARATED_TRANSFORMS[field(index, 12)] ?? "raw",
      depth: field(index, 13),
      logicalRowCount: field(index, 14),
      logicalColumnCount: field(index, 15),
      grammarMilli: field(index, 16),
      superseded: Boolean(field(index, 17)),
    };
  }

  jobs(): readonly SeparatedOcrJob[] {
    return Array.from({ length: this.jobCount() }, (_unused, index) =>
      this.job(index),
    );
  }

  raster(index: number): SeparatedOcrRaster {
    this.assertOpen();
    const field = (fieldIndex: number) => {
      const value = this.exports.ittm_separated_job_raster_field(
        this.handle,
        index,
        fieldIndex,
      );
      if (value <= 0) {
        throw new Error(
          `Invalid separated OCR raster field: ${index}:${fieldIndex}`,
        );
      }
      return value;
    };
    const width = field(0);
    const height = field(1);
    const stride = field(2);
    const format = field(3);
    if (format !== 3 || stride !== width * format) {
      throw new Error("Separated OCR raster is not packed RGB");
    }
    const length = this.exports.ittm_separated_job_raster_length(
      this.handle,
      index,
    );
    if (length !== stride * height) {
      throw new Error(
        "Separated OCR raster length disagrees with its dimensions",
      );
    }
    const pointer = this.exports.ittm_alloc(length) >>> 0;
    if (!pointer)
      throw new Error("Pipeline core could not allocate raster buffer");
    try {
      const copied = this.exports.ittm_separated_job_raster_copy(
        this.handle,
        index,
        pointer,
        length,
      );
      if (copied !== length) {
        throw new Error(
          `Separated OCR raster copied ${copied} bytes; expected ${length}`,
        );
      }
      return {
        pixels: new Uint8Array(
          this.exports.memory.buffer,
          pointer,
          length,
        ).slice(),
        width,
        height,
        stride,
        format: 3,
      };
    } finally {
      this.exports.ittm_dealloc(pointer, length);
    }
  }

  completedStages(): readonly (typeof SEPARATED_PIPELINE_STAGES)[number][] {
    this.assertOpen();
    const mask = this.exports.ittm_separated_stage_mask(this.handle);
    return SEPARATED_PIPELINE_STAGES.filter((_stage, index) =>
      Boolean(mask & (1 << index)),
    );
  }

  setOcr(index: number, text: string, confidenceMilli = 0): void {
    this.assertOpen();
    const encoded = new TextEncoder().encode(text);
    const pointer = copyIntoWasm(this.exports, encoded);
    try {
      const status = this.exports.ittm_separated_set_ocr(
        this.handle,
        index,
        pointer,
        encoded.byteLength,
        Math.max(0, Math.min(1_000, Math.floor(confidenceMilli))),
      );
      if (status !== 0) {
        throw new Error(`Separated OCR handoff failed with status ${status}`);
      }
    } finally {
      if (pointer) this.exports.ittm_dealloc(pointer, encoded.byteLength);
    }
  }

  addOcrWord(index: number, word: SeparatedOcrWord): void {
    this.assertOpen();
    const encoded = new TextEncoder().encode(word.text);
    const pointer = copyIntoWasm(this.exports, encoded);
    try {
      const status = this.exports.ittm_separated_add_ocr_word_ppm(
        this.handle,
        index,
        pointer,
        encoded.byteLength,
        ...word.bbox,
        Math.max(
          0,
          Math.min(1_000_000, Math.floor(word.confidenceMilli * 1_000)),
        ),
      );
      if (status !== 0) {
        throw new Error(
          `Separated OCR word handoff failed with status ${status}`,
        );
      }
    } finally {
      if (pointer) this.exports.ittm_dealloc(pointer, encoded.byteLength);
    }
  }

  render(): string {
    this.assertOpen();
    const length = this.exports.ittm_separated_render_length(this.handle);
    if (!length) return "";
    const pointer = this.exports.ittm_alloc(length) >>> 0;
    if (!pointer)
      throw new Error("Pipeline core could not allocate render buffer");
    try {
      const copied = this.exports.ittm_separated_render_copy(
        this.handle,
        pointer,
        length,
      );
      if (copied !== length) {
        throw new Error(
          `Separated renderer copied ${copied} bytes; expected ${length}`,
        );
      }
      return new TextDecoder().decode(
        new Uint8Array(this.exports.memory.buffer, pointer, length),
      );
    } finally {
      this.exports.ittm_dealloc(pointer, length);
    }
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.exports.ittm_separated_drop(this.handle) !== 0) {
      throw new Error(`Unknown separated pipeline handle: ${this.handle}`);
    }
  }

  private assertOpen(): void {
    if (this.closed) throw new Error("Separated pipeline session is closed");
  }
}

let corePromise: Promise<BrowserPipelineCore> | undefined;
let configuredCoreUrl: string | undefined;

export function configureBrowserPipelineCoreUrl(url: string): void {
  if (!url) throw new Error("Pipeline core URL must not be empty");
  if (configuredCoreUrl !== url) corePromise = undefined;
  configuredCoreUrl = url;
}

function defaultCoreUrl(): string {
  const viteBase = (import.meta as ImportMeta & { env?: { BASE_URL?: string } })
    .env?.BASE_URL;
  const base = viteBase || "/";
  return `${base.endsWith("/") ? base : `${base}/`}wasm/ittm_pipeline_core.wasm`;
}

export function loadBrowserPipelineCore(
  url = configuredCoreUrl ?? defaultCoreUrl(),
): Promise<BrowserPipelineCore> {
  if (!corePromise) {
    corePromise = fetch(url)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(
            `Could not load pipeline core: HTTP ${response.status}`,
          );
        }
        const bytes = await response.arrayBuffer();
        return WebAssembly.instantiate(bytes, {});
      })
      .then(
        ({ instance }) =>
          new BrowserPipelineCore(instance.exports as PipelineCoreExports),
      );
  }
  return corePromise;
}
