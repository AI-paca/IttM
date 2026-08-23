import type { AppDiagnostics } from "./types";
import type { BrowserPipelineProfile } from "./pipeline-config";
import {
  BROWSER_PIPELINE_PROFILES,
  normalizeBrowserPipelineProfile,
} from "./pipeline-config";

export const STRICT_LANGUAGES = "rus+eng+chi_sim";

export interface BrowserOcrProfile {
  languages: string;
  cacheWorker: boolean;
  maxImagePixels: number;
  maxDimension: number;
  pdfRenderScale: number;
  reason: string;
  preprocessingProfile: string;
  imagePreprocessing: BrowserPipelineProfile["imagePreprocessing"];
  textRegionPsm: string;
  denseGridFallback: boolean;
  spatialFullPageFallback: boolean;
  darkUiTextFallback: boolean;
  contextualMarkdownGrammar: boolean;
  denseGridTargetWidth: number;
  ocrBorderPixels: number;
  edgeWordFallbackPsm: string;
  edgeWordFallbackMinTokens: number;
  lexicalCorrection: BrowserPipelineProfile["lexicalCorrection"];
  ocrLanguageRetry: BrowserPipelineProfile["ocrLanguageRetry"];
  tableSlotBuilder: BrowserPipelineProfile["tableSlotBuilder"];
  tableSlotMaxColumns: number;
  recursiveTableCellOcr: BrowserPipelineProfile["recursiveTableCellOcr"];
  recursiveTableCellOcrBatchPixels: number;
  layout: BrowserPipelineProfile["layout"];
  langPath?: string;
  cachePath?: string;
  gzip?: boolean;
  availableLanguages?: string[];
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function createBrowserOcrProfile(
  diagnostics: AppDiagnostics | null,
  pipelineProfile: BrowserPipelineProfile = BROWSER_PIPELINE_PROFILES.browser_tesseract_standard,
): BrowserOcrProfile {
  const profile = normalizeBrowserPipelineProfile(pipelineProfile);
  const memory = numberOrNull(diagnostics?.browser.memory);
  const cores = numberOrNull(diagnostics?.browser.cores);
  const backendOffline = !diagnostics?.backend || Boolean(diagnostics?.error);

  if ((memory !== null && memory <= 2) || (cores !== null && cores <= 2)) {
    return {
      languages: profile.languages || STRICT_LANGUAGES,
      // Reuse the bounded worker; rebuilding it for every PDF page costs more
      // memory and time than keeping its already-loaded language data alive.
      cacheWorker: true,
      maxImagePixels: 4_000_000,
      maxDimension: 2200,
      pdfRenderScale: 1,
      reason: "low-memory-browser",
      preprocessingProfile: profile.name,
      imagePreprocessing: profile.imagePreprocessing,
      textRegionPsm: profile.textRegionPsm,
      denseGridFallback: profile.denseGridFallback,
      spatialFullPageFallback: profile.spatialFullPageFallback,
      darkUiTextFallback: profile.darkUiTextFallback,
      contextualMarkdownGrammar: profile.contextualMarkdownGrammar,
      denseGridTargetWidth: profile.denseGridTargetWidth,
      ocrBorderPixels: profile.ocrBorderPixels,
      edgeWordFallbackPsm: profile.edgeWordFallbackPsm,
      edgeWordFallbackMinTokens: profile.edgeWordFallbackMinTokens,
      lexicalCorrection: profile.lexicalCorrection,
      ocrLanguageRetry: profile.ocrLanguageRetry,
      tableSlotBuilder: profile.tableSlotBuilder,
      tableSlotMaxColumns: profile.tableSlotMaxColumns,
      recursiveTableCellOcr: profile.recursiveTableCellOcr,
      recursiveTableCellOcrBatchPixels:
        profile.recursiveTableCellOcrBatchPixels,
      layout: profile.layout,
    };
  }

  if (backendOffline || (memory !== null && memory <= 4)) {
    return {
      languages: profile.languages || STRICT_LANGUAGES,
      cacheWorker: true,
      maxImagePixels: 8_000_000,
      maxDimension: 3200,
      pdfRenderScale: 1.25,
      reason: "balanced-browser-fallback",
      preprocessingProfile: profile.name,
      imagePreprocessing: profile.imagePreprocessing,
      textRegionPsm: profile.textRegionPsm,
      denseGridFallback: profile.denseGridFallback,
      spatialFullPageFallback: profile.spatialFullPageFallback,
      darkUiTextFallback: profile.darkUiTextFallback,
      contextualMarkdownGrammar: profile.contextualMarkdownGrammar,
      denseGridTargetWidth: profile.denseGridTargetWidth,
      ocrBorderPixels: profile.ocrBorderPixels,
      edgeWordFallbackPsm: profile.edgeWordFallbackPsm,
      edgeWordFallbackMinTokens: profile.edgeWordFallbackMinTokens,
      lexicalCorrection: profile.lexicalCorrection,
      ocrLanguageRetry: profile.ocrLanguageRetry,
      tableSlotBuilder: profile.tableSlotBuilder,
      tableSlotMaxColumns: profile.tableSlotMaxColumns,
      recursiveTableCellOcr: profile.recursiveTableCellOcr,
      recursiveTableCellOcrBatchPixels:
        profile.recursiveTableCellOcrBatchPixels,
      layout: profile.layout,
    };
  }

  return {
    languages: profile.languages || STRICT_LANGUAGES,
    cacheWorker: true,
    maxImagePixels: 14_000_000,
    maxDimension: 4200,
    pdfRenderScale: 1.5,
    reason: "quality-first",
    preprocessingProfile: profile.name,
    imagePreprocessing: profile.imagePreprocessing,
    textRegionPsm: profile.textRegionPsm,
    denseGridFallback: profile.denseGridFallback,
    spatialFullPageFallback: profile.spatialFullPageFallback,
    darkUiTextFallback: profile.darkUiTextFallback,
    contextualMarkdownGrammar: profile.contextualMarkdownGrammar,
    denseGridTargetWidth: profile.denseGridTargetWidth,
    ocrBorderPixels: profile.ocrBorderPixels,
    edgeWordFallbackPsm: profile.edgeWordFallbackPsm,
    edgeWordFallbackMinTokens: profile.edgeWordFallbackMinTokens,
    lexicalCorrection: profile.lexicalCorrection,
    ocrLanguageRetry: profile.ocrLanguageRetry,
    tableSlotBuilder: profile.tableSlotBuilder,
    tableSlotMaxColumns: profile.tableSlotMaxColumns,
    recursiveTableCellOcr: profile.recursiveTableCellOcr,
    recursiveTableCellOcrBatchPixels: profile.recursiveTableCellOcrBatchPixels,
    layout: profile.layout,
  };
}
