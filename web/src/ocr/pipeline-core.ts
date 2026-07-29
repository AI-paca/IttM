export const PIPELINE_CORE_ABI_VERSION = 3;

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
}

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
