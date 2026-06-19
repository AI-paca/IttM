import {
  BROWSER_PIPELINE_PROFILES,
  browserPipelineProfileForSource,
} from "../../web/src/ocr/pipeline-config";

export const DEFAULT_BROWSER_BENCHMARK_PROFILE =
  browserPipelineProfileForSource("browser").name;

export function resolveBrowserBenchmarkProfile(profileName?: string) {
  const selected = profileName || DEFAULT_BROWSER_BENCHMARK_PROFILE;
  const profile = BROWSER_PIPELINE_PROFILES[selected];
  if (!profile) {
    const knownProfiles = Object.keys(BROWSER_PIPELINE_PROFILES)
      .sort()
      .join(", ");
    throw new Error(
      `Unknown browser OCR profile '${selected}'. Known profiles: ${knownProfiles}`,
    );
  }
  return profile;
}
