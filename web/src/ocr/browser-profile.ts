import type { AppDiagnostics } from "./types";
import type { BrowserPipelineProfile } from "./pipeline-config";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";

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
  denseGridTargetWidth: number;
  ocrBorderPixels: number;
  edgeWordFallbackPsm: string;
  edgeWordFallbackMinTokens: number;
  layout: BrowserPipelineProfile["layout"];
  langPath?: string;
  cachePath?: string;
  gzip?: boolean;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function createBrowserOcrProfile(
  diagnostics: AppDiagnostics | null,
  pipelineProfile: BrowserPipelineProfile = BROWSER_PIPELINE_PROFILES.browser_tesseract_standard,
): BrowserOcrProfile {
  const memory = numberOrNull(diagnostics?.browser.memory);
  const cores = numberOrNull(diagnostics?.browser.cores);
  const backendOffline = !diagnostics?.backend || Boolean(diagnostics?.error);

  if ((memory !== null && memory <= 2) || (cores !== null && cores <= 2)) {
    return {
      languages: STRICT_LANGUAGES,
      cacheWorker: false,
      maxImagePixels: 4_000_000,
      maxDimension: 2200,
      pdfRenderScale: 1,
      reason: "low-memory-browser",
      preprocessingProfile: pipelineProfile.name,
      imagePreprocessing: pipelineProfile.imagePreprocessing,
      textRegionPsm: pipelineProfile.textRegionPsm,
      denseGridFallback: pipelineProfile.denseGridFallback,
      denseGridTargetWidth: pipelineProfile.denseGridTargetWidth,
      ocrBorderPixels: pipelineProfile.ocrBorderPixels,
      edgeWordFallbackPsm: pipelineProfile.edgeWordFallbackPsm,
      edgeWordFallbackMinTokens: pipelineProfile.edgeWordFallbackMinTokens,
      layout: pipelineProfile.layout,
    };
  }

  if (backendOffline || (memory !== null && memory <= 4)) {
    return {
      languages: STRICT_LANGUAGES,
      cacheWorker: true,
      maxImagePixels: 8_000_000,
      maxDimension: 3200,
      pdfRenderScale: 1.25,
      reason: "balanced-browser-fallback",
      preprocessingProfile: pipelineProfile.name,
      imagePreprocessing: pipelineProfile.imagePreprocessing,
      textRegionPsm: pipelineProfile.textRegionPsm,
      denseGridFallback: pipelineProfile.denseGridFallback,
      denseGridTargetWidth: pipelineProfile.denseGridTargetWidth,
      ocrBorderPixels: pipelineProfile.ocrBorderPixels,
      edgeWordFallbackPsm: pipelineProfile.edgeWordFallbackPsm,
      edgeWordFallbackMinTokens: pipelineProfile.edgeWordFallbackMinTokens,
      layout: pipelineProfile.layout,
    };
  }

  return {
    languages: STRICT_LANGUAGES,
    cacheWorker: true,
    maxImagePixels: 14_000_000,
    maxDimension: 4200,
    pdfRenderScale: 1.5,
    reason: "quality-first",
    preprocessingProfile: pipelineProfile.name,
    imagePreprocessing: pipelineProfile.imagePreprocessing,
    textRegionPsm: pipelineProfile.textRegionPsm,
    denseGridFallback: pipelineProfile.denseGridFallback,
    denseGridTargetWidth: pipelineProfile.denseGridTargetWidth,
    ocrBorderPixels: pipelineProfile.ocrBorderPixels,
    edgeWordFallbackPsm: pipelineProfile.edgeWordFallbackPsm,
    edgeWordFallbackMinTokens: pipelineProfile.edgeWordFallbackMinTokens,
    layout: pipelineProfile.layout,
  };
}
