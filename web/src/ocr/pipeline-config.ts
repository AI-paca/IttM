import type { SourceType } from "./types";
import type { BrowserLayoutPipelineConfig } from "./layout-contracts";

export type ImagePreprocessStepId =
  | "projector_slide_dewarp"
  | "projected_document_dewarp"
  | "ocr_border"
  | "browser_resize";

export interface BrowserPipelineProfile {
  name: string;
  languages?: string;
  imagePreprocessing: ImagePreprocessStepId[];
  textRegionPsm: string;
  denseGridFallback: boolean;
  spatialFullPageFallback: boolean;
  darkUiTextFallback: boolean;
  contextualMarkdownGrammar: boolean;
  denseGridTargetWidth: number;
  ocrBorderPixels: number;
  edgeWordFallbackPsm: string;
  edgeWordFallbackMinTokens: number;
  lexicalCorrection: "off" | "t9_small";
  ocrLanguageRetry: "off" | "t9_small";
  tableSlotBuilder: "off" | "line_merge_v1" | "recursive_gaps_v1";
  tableSlotMaxColumns: number;
  recursiveTableCellOcr: "off" | "auto" | "always";
  recursiveTableCellOcrBatchPixels: number;
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
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "off",
      tableSlotMaxColumns: 4,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
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
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "off",
      tableSlotMaxColumns: 4,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
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
      spatialFullPageFallback: false,
      darkUiTextFallback: false,
      contextualMarkdownGrammar: false,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "off",
      tableSlotMaxColumns: 4,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
      layout: {
        featureExtractors: [],
        selector: "fixed",
        allowedStages: [],
        defaultParameters: {},
      },
    },
    browser_tesseract_table_first: {
      name: "browser_tesseract_table_first",
      imagePreprocessing: [
        "projector_slide_dewarp",
        "projected_document_dewarp",
        "browser_resize",
        "ocr_border",
      ],
      textRegionPsm: "6",
      denseGridFallback: true,
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "off",
      tableSlotMaxColumns: 4,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
      layout: {
        featureExtractors: ["projection_geometry"],
        selector: "table_first_heuristic_v1",
        allowedStages: ["table_regions", "spatial_regions"],
        defaultParameters: {
          maxRegionHeight: 1400,
          minRegionHeight: 300,
          minSeparatorCoverage: 0.55,
        },
      },
    },
    browser_tesseract_table_slots: {
      name: "browser_tesseract_table_slots",
      imagePreprocessing: [
        "projector_slide_dewarp",
        "projected_document_dewarp",
        "browser_resize",
        "ocr_border",
      ],
      textRegionPsm: "6",
      denseGridFallback: false,
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "recursive_gaps_v1",
      tableSlotMaxColumns: 14,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
      layout: {
        featureExtractors: ["projection_geometry"],
        selector: "table_first_heuristic_v1",
        allowedStages: ["table_regions", "spatial_regions"],
        defaultParameters: {
          maxRegionHeight: 1400,
          minRegionHeight: 300,
          minSeparatorCoverage: 0.55,
        },
      },
    },
    browser_tesseract_table_slots_t9: {
      name: "browser_tesseract_table_slots_t9",
      imagePreprocessing: [
        "projector_slide_dewarp",
        "projected_document_dewarp",
        "browser_resize",
        "ocr_border",
      ],
      textRegionPsm: "6",
      denseGridFallback: false,
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "t9_small",
      ocrLanguageRetry: "t9_small",
      tableSlotBuilder: "recursive_gaps_v1",
      tableSlotMaxColumns: 14,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
      layout: {
        featureExtractors: ["projection_geometry"],
        selector: "table_first_heuristic_v1",
        allowedStages: ["table_regions", "spatial_regions"],
        defaultParameters: {
          maxRegionHeight: 1400,
          minRegionHeight: 300,
          minSeparatorCoverage: 0.55,
        },
      },
    },
    browser_tesseract_greek_math: {
      name: "browser_tesseract_greek_math",
      languages: "rus+eng+ell+equ",
      imagePreprocessing: [
        "projector_slide_dewarp",
        "projected_document_dewarp",
        "browser_resize",
        "ocr_border",
      ],
      textRegionPsm: "6",
      denseGridFallback: true,
      spatialFullPageFallback: true,
      darkUiTextFallback: true,
      contextualMarkdownGrammar: true,
      denseGridTargetWidth: 3300,
      ocrBorderPixels: 10,
      edgeWordFallbackPsm: "7",
      edgeWordFallbackMinTokens: 1,
      lexicalCorrection: "off",
      ocrLanguageRetry: "off",
      tableSlotBuilder: "off",
      tableSlotMaxColumns: 4,
      recursiveTableCellOcr: "auto",
      recursiveTableCellOcrBatchPixels: 8_000_000,
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
  lexicalCorrectionEnabled = false,
): Record<string, string> | undefined {
  const profile = SOURCE_PIPELINE_PROFILES[source]?.backendProfile;
  if (!profile) return undefined;
  return {
    pipeline_profile: profile,
    ...(lexicalCorrectionEnabled
      ? {
          pipeline_flags:
            "lexical_correction:t9_small;ocr_language_retry:t9_small",
        }
      : {}),
  };
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
