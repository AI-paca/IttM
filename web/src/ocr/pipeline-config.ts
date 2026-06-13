import type { SourceType } from "./types";

export type ImagePreprocessStepId =
  | "projected_document_dewarp"
  | "browser_resize";

export interface BrowserPipelineProfile {
  name: string;
  imagePreprocessing: ImagePreprocessStepId[];
}

export interface SourcePipelineProfile {
  backendProfile?: string;
  browserProfile?: string;
}

export const BROWSER_PIPELINE_PROFILES: Record<string, BrowserPipelineProfile> =
  {
    browser_tesseract_standard: {
      name: "browser_tesseract_standard",
      imagePreprocessing: ["browser_resize"],
    },
    browser_tesseract_dewarp: {
      name: "browser_tesseract_dewarp",
      imagePreprocessing: ["projected_document_dewarp", "browser_resize"],
    },
    browser_tesseract_raw: {
      name: "browser_tesseract_raw",
      imagePreprocessing: ["browser_resize"],
    },
  };

export const SOURCE_PIPELINE_PROFILES: Record<
  SourceType,
  SourcePipelineProfile
> = {
  auto: { backendProfile: "backend_auto_standard" },
  gateway: { backendProfile: "backend_auto_standard" },
  browser: { browserProfile: "browser_tesseract_standard" },
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
