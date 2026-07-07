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

function countSeparators(
  features: LayoutFeatures,
  axis: "x" | "y",
  kind: "ink" | "whitespace",
  minStrength: number,
): number {
  return features.separators.filter(
    (separator) =>
      separator.axis === axis &&
      separator.kind === kind &&
      separator.strength >= minStrength,
  ).length;
}

const tableFirstHeuristicSelector: LayoutSelector = (features, config) => {
  const aspectRatio =
    typeof features.scalars.aspectRatio === "number"
      ? features.scalars.aspectRatio
      : features.height / Math.max(1, features.width);
  const horizontalInk = countSeparators(features, "y", "ink", 0.18);
  const verticalInk = countSeparators(features, "x", "ink", 0.18);
  const horizontalWhitespace = countSeparators(
    features,
    "y",
    "whitespace",
    0.5,
  );
  const verticalWhitespace = countSeparators(features, "x", "whitespace", 0.5);
  const textGridScore = Math.min(
    1,
    Math.min(horizontalInk / 8, 1) * 0.45 +
      Math.min(verticalInk / 6, 1) * 0.45 +
      Math.min(verticalWhitespace / 6, 1) * 0.1,
  );
  const parameters = {
    minSourceWidth: 0,
    maxSourceWidth: "infinity",
    ...config.defaultParameters,
    aspectRatio: Number(aspectRatio.toFixed(3)),
    layoutClass: "spatial_blocks",
    mediumPageSegmentation: true,
    multiColumnTextRows: verticalWhitespace,
    textColumnTracks: verticalInk + verticalWhitespace,
    textGridScore: Number(textGridScore.toFixed(3)),
    textRowTracks: horizontalInk + horizontalWhitespace,
  };

  if (features.scalars.extractorAvailable === false) {
    return { label: "analysis_unavailable", stages: [], confidence: 0 };
  }
  if (features.foregroundRatio <= 0) {
    return { label: "empty", stages: [], confidence: 1 };
  }

  const hasTableStage = config.allowedStages.includes("table_regions");
  const hasSpatialStage = config.allowedStages.includes("spatial_regions");
  const tableLike = horizontalInk >= 3 && verticalInk >= 2;
  const denseTableLike =
    horizontalInk >= 8 && (verticalInk >= 1 || textGridScore >= 0.45);

  if (hasTableStage && (tableLike || denseTableLike)) {
    return {
      label: "table_like_grid",
      stages: [
        {
          name: "table_regions",
          parameters: { ...parameters, layoutClass: "table_like_grid" },
        },
      ],
      confidence: Math.max(0.55, textGridScore),
    };
  }

  if (hasSpatialStage) {
    const layoutClass =
      aspectRatio >= 4
        ? "long_vertical_blocks"
        : verticalWhitespace >= 2
          ? "multi_column_blocks"
          : "spatial_blocks";
    return {
      label: layoutClass,
      stages: [
        {
          name: "spatial_regions",
          parameters: { ...parameters, layoutClass },
        },
      ],
      confidence: 0.65,
    };
  }

  return { label: "unsegmented", stages: [], confidence: 0 };
};

const selectors: Readonly<Record<string, LayoutSelector>> = {
  fixed: fixedSelector,
  table_first_heuristic_v1: tableFirstHeuristicSelector,
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
