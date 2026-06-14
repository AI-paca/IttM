import type {
  LayoutDecision,
  LayoutFeatures,
  LayoutStageSpec,
  SeparatorCandidate,
} from "./layout-contracts";

export interface SourceRegion {
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
}

function numericParameter(
  stage: LayoutStageSpec,
  name: string,
  fallback: number,
): number {
  const value = stage.parameters[name];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function separatorCenter(separator: SeparatorCandidate): number {
  return Math.round((separator.start + separator.end) / 2);
}

function boundedHorizontalBands(
  features: LayoutFeatures,
  minHeight: number,
  maxHeight: number,
): Array<[number, number]> {
  const centers = new Set(
    features.separators
      .filter(
        (separator) =>
          separator.axis === "y" &&
          separator.kind === "whitespace" &&
          separator.strength >= 0.5,
      )
      .map(separatorCenter)
      .filter((center) => center > 0 && center < features.height),
  );
  const structuralCenters = new Set<number>();
  for (const component of features.components) {
    const [left, , right, bottom] = component.bbox;
    if (right - left < features.width * 0.55) continue;
    if (bottom > 0 && bottom < features.height) {
      centers.add(bottom);
      structuralCenters.add(bottom);
    }
  }
  const sortedCenters = Array.from(centers).sort((left, right) => left - right);
  if (features.height <= maxHeight) return [[0, features.height]];

  const bands: Array<[number, number]> = [];
  let cursor = 0;
  while (cursor < features.height) {
    if (features.height - cursor <= maxHeight) {
      bands.push([cursor, features.height]);
      break;
    }

    const lower = cursor + minHeight;
    const upper = Math.min(features.height, cursor + maxHeight);
    const structural = Array.from(structuralCenters).filter(
      (center) =>
        center >= cursor + Math.max(80, Math.floor(minHeight / 3)) &&
        center <= upper,
    );
    const candidates = sortedCenters.filter(
      (center) => center >= lower && center <= upper,
    );
    const cut =
      structural.length > 0
        ? Math.min(...structural)
        : candidates.length > 0
          ? Math.max(...candidates)
          : upper;
    if (cut <= cursor) break;
    bands.push([cursor, cut]);
    cursor = cut;
  }
  return bands;
}

function clusterSeparators(
  separators: readonly SeparatorCandidate[],
  tolerance: number,
): SeparatorCandidate[][] {
  const clusters: SeparatorCandidate[][] = [];
  for (const separator of [...separators].sort(
    (left, right) => separatorCenter(left) - separatorCenter(right),
  )) {
    const cluster = clusters.at(-1);
    if (!cluster) {
      clusters.push([separator]);
      continue;
    }
    const center =
      cluster.reduce((total, value) => total + separatorCenter(value), 0) /
      cluster.length;
    if (Math.abs(separatorCenter(separator) - center) <= tolerance) {
      cluster.push(separator);
    } else {
      clusters.push([separator]);
    }
  }
  return clusters;
}

function intervalCoverage(
  intervals: ReadonlyArray<readonly [number, number]>,
  start: number,
  end: number,
): number {
  const clipped = intervals
    .filter(([left, right]) => right > start && left < end)
    .map(
      ([left, right]) =>
        [Math.max(start, left), Math.min(end, right)] as [number, number],
    )
    .sort((left, right) => left[0] - right[0]);
  if (clipped.length === 0) return 0;

  let total = 0;
  let [currentStart, currentEnd] = clipped[0];
  for (const [left, right] of clipped.slice(1)) {
    if (left <= currentEnd) {
      currentEnd = Math.max(currentEnd, right);
      continue;
    }
    total += Math.max(0, currentEnd - currentStart);
    currentStart = left;
    currentEnd = right;
  }
  total += Math.max(0, currentEnd - currentStart);
  return total / Math.max(1, end - start);
}

function verticalCutsForBand(
  features: LayoutFeatures,
  top: number,
  bottom: number,
  minRegionWidth: number,
  minCoverage: number,
): number[] {
  const candidates = features.separators.filter(
    (separator) =>
      separator.axis === "x" &&
      separator.kind === "whitespace" &&
      separator.strength >= 0.5 &&
      separator.spanEnd > top &&
      separator.spanStart < bottom,
  );
  const summaries = clusterSeparators(
    candidates,
    Math.max(4, Math.round(features.width * 0.05)),
  )
    .map((cluster) => {
      const coverage = intervalCoverage(
        cluster.map((separator) => [separator.spanStart, separator.spanEnd]),
        top,
        bottom,
      );
      const center = Math.round(
        cluster.reduce(
          (total, separator) => total + separatorCenter(separator),
          0,
        ) / cluster.length,
      );
      const widths = cluster
        .map((separator) => separator.end - separator.start)
        .sort((left, right) => left - right);
      const gapWidth = widths[Math.floor(widths.length / 2)];
      return { center, coverage, gapWidth };
    })
    .filter(
      ({ coverage, gapWidth }) =>
        coverage >= minCoverage &&
        gapWidth >= Math.max(8, features.width * 0.01),
    );
  if (summaries.length === 0) return [];

  const crossesWideComponent = (cut: number) =>
    features.components.some((component) => {
      const [left, componentTop, right, componentBottom] = component.bbox;
      const overlap =
        Math.min(bottom, componentBottom) - Math.max(top, componentTop);
      return (
        overlap >= (bottom - top) * 0.1 &&
        left < cut &&
        cut < right &&
        right - left >= features.width * 0.3
      );
    });
  const strongest = Math.max(
    ...summaries.map(({ coverage, gapWidth }) => coverage * gapWidth),
  );
  const cuts = summaries
    .filter(
      ({ center, coverage, gapWidth }) =>
        coverage * gapWidth >= strongest * 0.65 &&
        !crossesWideComponent(center),
    )
    .map(({ center }) => center)
    .sort((left, right) => left - right);

  const selected: number[] = [];
  let previous = 0;
  for (const cut of cuts) {
    if (cut - previous < minRegionWidth) continue;
    if (features.width - cut < minRegionWidth) continue;
    selected.push(cut);
    previous = cut;
  }
  return selected;
}

function spatialRegions(
  features: LayoutFeatures,
  stage: LayoutStageSpec,
): SourceRegion[] {
  const maxRegionHeight = Math.max(
    200,
    Math.round(numericParameter(stage, "maxRegionHeight", 1400)),
  );
  const minRegionHeight = Math.max(
    80,
    Math.min(
      maxRegionHeight,
      Math.round(numericParameter(stage, "minRegionHeight", 300)),
    ),
  );
  const minRegionWidth = Math.max(
    40,
    Math.round(
      numericParameter(
        stage,
        "minRegionWidth",
        Math.max(80, features.width * 0.08),
      ),
    ),
  );
  const minCoverage = Math.max(
    0.05,
    Math.min(1, numericParameter(stage, "minSeparatorCoverage", 0.55)),
  );

  const regions: SourceRegion[] = [];
  for (const [top, bottom] of boundedHorizontalBands(
    features,
    minRegionHeight,
    maxRegionHeight,
  )) {
    const cuts = verticalCutsForBand(
      features,
      top,
      bottom,
      minRegionWidth,
      minCoverage,
    );
    const boundaries = [0, ...cuts, features.width];
    for (let index = 0; index < boundaries.length - 1; index += 1) {
      regions.push({
        sourceX: boundaries[index],
        sourceY: top,
        sourceWidth: boundaries[index + 1] - boundaries[index],
        sourceHeight: bottom - top,
      });
    }
  }
  return regions;
}

export function executeBrowserLayout(
  features: LayoutFeatures,
  decision: LayoutDecision,
): SourceRegion[] {
  const wholeImage: SourceRegion = {
    sourceX: 0,
    sourceY: 0,
    sourceWidth: features.width,
    sourceHeight: features.height,
  };
  if (decision.stages.length === 0) return [wholeImage];

  const stage = decision.stages[0];
  if (stage.name === "spatial_regions") {
    const regions = spatialRegions(features, stage);
    return regions.length > 0 ? regions : [wholeImage];
  }
  throw new Error(`Unknown browser layout stage '${stage.name}'.`);
}
