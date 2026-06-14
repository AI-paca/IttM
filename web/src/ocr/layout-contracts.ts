export type LayoutFeatureValue = boolean | number | string;

export interface SeparatorCandidate {
  axis: "x" | "y";
  start: number;
  end: number;
  spanStart: number;
  spanEnd: number;
  kind: "ink" | "whitespace";
  strength: number;
}

export interface ComponentFeature {
  bbox: readonly [number, number, number, number];
  area: number;
  fillRatio: number;
}

export interface LayoutFeatures {
  width: number;
  height: number;
  foregroundRatio: number;
  separators: readonly SeparatorCandidate[];
  components: readonly ComponentFeature[];
  scalars: Readonly<Record<string, LayoutFeatureValue>>;
}

export interface LayoutStageSpec {
  name: string;
  parameters: Readonly<Record<string, LayoutFeatureValue>>;
}

export interface LayoutDecision {
  label: string;
  stages: readonly LayoutStageSpec[];
  confidence: number;
}

export interface BrowserLayoutPipelineConfig {
  featureExtractors: readonly string[];
  selector: string;
  allowedStages: readonly string[];
  defaultParameters: Readonly<Record<string, LayoutFeatureValue>>;
}
