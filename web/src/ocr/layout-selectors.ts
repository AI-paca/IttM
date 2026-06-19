import type {
  BrowserLayoutPipelineConfig,
  LayoutDecision,
  LayoutFeatures,
} from "./layout-contracts";

type LayoutSelector = (
  features: LayoutFeatures,
  config: BrowserLayoutPipelineConfig,
) => LayoutDecision;

const fixedSelector: LayoutSelector = (_features, config) => ({
  label: "fixed",
  stages: config.allowedStages.map((name) => ({
    name,
    parameters: config.defaultParameters,
  })),
  confidence: 1,
});

const uniformSpatialSelector: LayoutSelector = (features, config) => {
  if (features.foregroundRatio <= 0) {
    return { label: "empty", stages: [], confidence: 1 };
  }
  if (!config.allowedStages.includes("spatial_regions")) {
    return { label: "unsegmented", stages: [], confidence: 1 };
  }

  return {
    label: "spatial",
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          minSourceWidth: 0,
          maxSourceWidth: "infinity",
          ...config.defaultParameters,
        },
      },
    ],
    confidence: 1,
  };
};

const selectors: Readonly<Record<string, LayoutSelector>> = {
  fixed: fixedSelector,
  uniform_spatial_v1: uniformSpatialSelector,
};

export function selectBrowserLayout(
  features: LayoutFeatures,
  config: BrowserLayoutPipelineConfig,
): LayoutDecision {
  const selector = selectors[config.selector];
  if (!selector) {
    throw new Error(`Unknown browser layout selector '${config.selector}'.`);
  }
  return selector(features, config);
}
