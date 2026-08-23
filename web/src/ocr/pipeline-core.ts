export const PIPELINE_CORE_ABI_VERSION = 4;

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

export interface SeparatedOcrJob {
  index: number;
  bbox: readonly [number, number, number, number];
  objectId: number;
  row: number;
  column: number;
  rowSpan: number;
  columnSpan: number;
}

export interface PipelineCapabilities {
  trustedText?: boolean;
  providesLayout?: boolean;
  providesMarkdown?: boolean;
  needsLanguageRetry?: boolean;
}

interface PipelineCoreExports extends WebAssembly.Exports {
  ittm_pipeline_abi_version(): number;
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
  ittm_separated_begin?(
    pointer: number,
    byteLength: number,
    width: number,
    height: number,
    stride: number,
    format: number,
  ): number;
  ittm_separated_job_count?(handle: number): number;
  ittm_separated_job_field?(
    handle: number,
    index: number,
    field: number,
  ): number;
  ittm_separated_set_ocr?(
    handle: number,
    index: number,
    pointer: number,
    byteLength: number,
    confidenceMilli: number,
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
    | "ittm_separated_job_count"
    | "ittm_separated_job_field"
    | "ittm_separated_set_ocr"
    | "ittm_separated_render_length"
    | "ittm_separated_render_copy"
    | "ittm_separated_stage_mask"
    | "ittm_separated_drop"
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
      handle = separated.ittm_separated_begin(
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
    "ittm_separated_job_count",
    "ittm_separated_job_field",
    "ittm_separated_set_ocr",
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

function copyIntoWasm(
  exports: SeparatedExports,
  bytes: Uint8Array | Uint8ClampedArray,
): number {
  if (bytes.byteLength === 0) return 0;
  const pointer = exports.ittm_alloc(bytes.byteLength);
  if (!pointer) throw new Error("Pipeline core could not allocate WASM memory");
  new Uint8Array(exports.memory.buffer, pointer, bytes.byteLength).set(bytes);
  return pointer;
}

export class BrowserSeparatedSession {
  private closed = false;

  constructor(
    private readonly exports: SeparatedExports,
    private readonly handle: number,
  ) {}

  jobs(): readonly SeparatedOcrJob[] {
    this.assertOpen();
    const count = this.exports.ittm_separated_job_count(this.handle);
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
    return Array.from({ length: count }, (_unused, index) => ({
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
    }));
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

  render(): string {
    this.assertOpen();
    const length = this.exports.ittm_separated_render_length(this.handle);
    if (!length) return "";
    const pointer = this.exports.ittm_alloc(length);
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
