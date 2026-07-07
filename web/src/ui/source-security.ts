import type { SourceType } from "../ocr/types";

export type SourceSafetyTier =
  | "on_device"
  | "variable"
  | "controlled_backend"
  | "external_cloud";

/**
 * Семантическая шкала безопасности источников.
 * Цвета стикеров выводятся из этих уровней, а не из произвольного цвета id.
 */
export const SOURCE_SAFETY_BY_SOURCE: Record<SourceType, SourceSafetyTier> = {
  browser: "on_device",
  auto: "variable",
  local_tess: "controlled_backend",
  local_easy: "controlled_backend",
  gateway: "controlled_backend",
  llm: "external_cloud",
};

export const SOURCE_SAFETY_TIER_SEEDS: Record<SourceSafetyTier, string> = {
  on_device: "#22A06B",
  variable: "#64748B",
  controlled_backend: "#D9921E",
  external_cloud: "#D94A45",
};

export function getSourceSafetyTier(id: string): SourceSafetyTier | null {
  return SOURCE_SAFETY_BY_SOURCE[id as SourceType] ?? null;
}
