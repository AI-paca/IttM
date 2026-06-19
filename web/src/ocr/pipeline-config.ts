import type { SourceType } from "./types";
import type { BrowserLayoutPipelineConfig } from "./layout-contracts";

export type ImagePreprocessStepId =
  | "projector_slide_dewarp"
  | "projected_document_dewarp"
  | "ocr_border"
  | "browser_resize";

export interface BrowserPipelineProfile {
  name: string;
  imagePreprocessing: ImagePreprocessStepId[];
  textRegionPsm: string;
  denseGridFallback: boolean;
  denseGridTargetWidth: number;
  ocrBorderPixels: number;
  edgeWordFallbackPsm: string;
  edgeWordFallbackMinTokens: number;
  layout: BrowserLayoutPipelineConfig;
}

export interface SourcePipelineProfile {
  backendProfile?: string;
  browserProfile?: string;
}

export const BROWSER_PIPELINE_PROFILES: Record<string, BrowserPipelineProfile> =
  {
    browser_tesseract_standard: {
      name: "browser_tesseract_standard",
      imagePreprocessing: ["browser_resize", "ocr_border"],
      textRegionPsm: "6",
      denseGridFallback: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      layout: {
        featureExtractors: ["projection_geometry"],
        selector: "uniform_spatial_v1",
        allowedStages: ["spatial_regions"],
        defaultParameters: {
          maxRegionHeight: 1400,
          minRegionHeight: 300,
          minSeparatorCoverage: 0.55,
        },
      },
    },
    browser_tesseract_dewarp: {
      name: "browser_tesseract_dewarp",
      imagePreprocessing: [
        "projector_slide_dewarp",
        "projected_document_dewarp",
        "browser_resize",
        "ocr_border",
      ],
      textRegionPsm: "6",
      denseGridFallback: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      layout: {
        featureExtractors: ["projection_geometry"],
        selector: "uniform_spatial_v1",
        allowedStages: ["spatial_regions"],
        defaultParameters: {
          maxRegionHeight: 1400,
          minRegionHeight: 300,
          minSeparatorCoverage: 0.55,
        },
      },
    },
    browser_tesseract_raw: {
      name: "browser_tesseract_raw",
      imagePreprocessing: ["browser_resize", "ocr_border"],
      textRegionPsm: "6",
      denseGridFallback: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      layout: {
        featureExtractors: [],
        selector: "fixed",
        allowedStages: [],
        defaultParameters: {},
      },
    },
  };

export const SOURCE_PIPELINE_PROFILES: Record<
  SourceType,
  SourcePipelineProfile
> = {
  auto: { backendProfile: "backend_auto_standard" },
  gateway: { backendProfile: "backend_auto_standard" },
  browser: { browserProfile: "browser_tesseract_dewarp" },
  local_tess: { backendProfile: "backend_tesseract_standard" },
  local_easy: { backendProfile: "backend_easyocr_standard" },
  llm: {},
};

export function backendPipelineParams(
  source: SourceType,
): Record<string, string> | undefined {
  const profile = SOURCE_PIPELINE_PROFILES[source]?.backendProfile;
  return profile ? { pipeline_profile: profile } : undefined;
}

export function browserPipelineProfileForSource(
  source: SourceType,
): BrowserPipelineProfile {
  const profileName =
    SOURCE_PIPELINE_PROFILES[source]?.browserProfile ||
    "browser_tesseract_standard";
  return (
    BROWSER_PIPELINE_PROFILES[profileName] ||
    BROWSER_PIPELINE_PROFILES.browser_tesseract_standard
  );
}
