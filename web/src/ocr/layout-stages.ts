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

function booleanParameter(
  stage: LayoutStageSpec,
  name: string,
  fallback: boolean,
): boolean {
  const value = stage.parameters[name];
  return typeof value === "boolean" ? value : fallback;
}

function separatorCenter(separator: SeparatorCandidate): number {
  return Math.floor((separator.start + separator.end) / 2);
}

function boundedHorizontalBands(
  features: LayoutFeatures,
  minHeight: number,
  maxHeight: number,
): Array<[number, number]> {
  const rowBands = componentRowBands(features, minHeight);
  if (features.height <= maxHeight && rowBands.length >= 3) return rowBands;

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

function componentRowBands(
  features: LayoutFeatures,
  minHeight: number,
): Array<[number, number]> {
  if (features.foregroundRatio < 0.045) return [];

  const maxComponentHeight = Math.max(24, Math.floor(features.height * 0.22));
  const minComponentWidth = Math.max(12, Math.floor(features.width * 0.025));
  const intervals = features.components
    .map((component) => {
      const [left, top, right, bottom] = component.bbox;
      return {
        top,
        bottom,
        width: right - left,
        height: bottom - top,
        fillRatio: component.fillRatio,
      };
    })
    .filter(
      ({ width, height, fillRatio }) =>
        width >= minComponentWidth &&
        height > 0 &&
        height <= maxComponentHeight &&
        fillRatio >= 0.03,
    )
    .map(({ top, bottom }) => [top, bottom] as [number, number])
    .sort((left, right) => left[0] - right[0]);
  if (intervals.length < 3) return [];

  const heights = intervals
    .map(([top, bottom]) => bottom - top)
    .sort((left, right) => left - right);
  const medianHeight = heights[Math.floor(heights.length / 2)];
  const mergeGap = Math.max(6, Math.round(medianHeight * 0.45));
  const clusters: Array<[number, number]> = [];
  for (const [top, bottom] of intervals) {
    const cluster = clusters.at(-1);
    if (!cluster || top > cluster[1] + mergeGap) {
      clusters.push([top, bottom]);
      continue;
    }
    cluster[1] = Math.max(cluster[1], bottom);
  }
  if (clusters.length < 3) return [];

  const minBandHeight =
    features.foregroundRatio < 0.08
      ? Math.max(24, Math.round(medianHeight * 1.2))
      : Math.max(32, Math.round(minHeight * 0.25));
  const boundaries = [0];
  for (let index = 0; index < clusters.length - 1; index += 1) {
    const cut = Math.round((clusters[index][1] + clusters[index + 1][0]) / 2);
    if (cut - boundaries.at(-1)! < minBandHeight) continue;
    if (features.height - cut < minBandHeight) continue;
    boundaries.push(cut);
  }
  boundaries.push(features.height);
  const bands = boundaries
    .slice(0, -1)
    .map((top, index) => [top, boundaries[index + 1]] as [number, number])
    .filter(([top, bottom]) => bottom > top);
  return bands.length >= 3 ? bands : [];
}

function mediumHorizontalBands(
  features: LayoutFeatures,
  minHeight: number,
  maxHeight: number,
): Array<[number, number]> {
  if (features.height > maxHeight) {
    return boundedHorizontalBands(features, minHeight, maxHeight);
  }

  const minMediumHeight = Math.max(60, Math.min(180, minHeight));
  const centers = new Set(
    features.separators
      .filter(
        (separator) =>
          separator.axis === "y" &&
          separator.kind === "whitespace" &&
          separator.strength >= 0.65 &&
          separator.spanEnd - separator.spanStart >= features.width * 0.75,
      )
      .map(separatorCenter)
      .filter((center) => center > 0 && center < features.height),
  );
  const boundaries = [0];
  for (const center of Array.from(centers).sort(
    (left, right) => left - right,
  )) {
    if (center - boundaries.at(-1)! < minMediumHeight) continue;
    if (features.height - center < minMediumHeight) continue;
    boundaries.push(center);
  }
  boundaries.push(features.height);
  const bands = boundaries
    .slice(0, -1)
    .map((top, index) => [top, boundaries[index + 1]] as [number, number])
    .filter(([top, bottom]) => bottom > top);
  return bands.length > 1 ? bands : [[0, features.height]];
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
  const whitespaceCandidates = features.separators.filter(
    (separator) =>
      separator.axis === "x" &&
      separator.spanEnd > top &&
      separator.spanStart < bottom &&
      separator.kind === "whitespace" &&
      separator.strength >= 0.4,
  );
  const inkCandidates = features.separators.filter(
    (separator) =>
      separator.axis === "x" &&
      separator.spanEnd > top &&
      separator.spanStart < bottom &&
      separator.kind === "ink" &&
      separator.spanEnd - separator.spanStart >= (bottom - top) * 0.8,
  );

  const summarizeCluster = (
    cluster: SeparatorCandidate[],
  ): {
    center: number;
    coverage: number;
    gapWidth: number;
    backedByLinePair: boolean;
    hasInkSupport: boolean;
  } | null => {
    const coverage = intervalCoverage(
      cluster.map((separator) => [separator.spanStart, separator.spanEnd]),
      top,
      bottom,
    );
    if (coverage < minCoverage) return null;
    const center = Math.round(
      cluster.reduce(
        (total, separator) => total + separatorCenter(separator),
        0,
      ) / cluster.length,
    );
    const whitespaceWidths = cluster
      .filter((separator) => separator.kind === "whitespace")
      .map((separator) => separator.end - separator.start)
      .sort((left, right) => left - right);
    const widths =
      whitespaceWidths.length > 0
        ? whitespaceWidths
        : cluster
            .map((separator) => separator.end - separator.start)
            .sort((left, right) => left - right);
    const gapWidth = widths[Math.floor(widths.length / 2)];
    const inkCount = cluster.filter(
      (separator) => separator.kind === "ink",
    ).length;
    const hasLinePairCut =
      inkCount >= 2 && coverage >= Math.max(0.8, minCoverage);
    const hasWhitespaceCut =
      whitespaceWidths.length > 0 &&
      gapWidth >= Math.max(8, features.width * 0.01);
    if (!hasWhitespaceCut && !hasLinePairCut) return null;
    return {
      center,
      coverage,
      gapWidth: Math.max(1, gapWidth),
      backedByLinePair: hasLinePairCut,
      hasInkSupport: inkCount > 0,
    };
  };

  const strictTolerance = Math.max(4, Math.round(features.width * 0.015));
  const looseTolerance = Math.max(
    strictTolerance,
    Math.round(features.width * 0.05),
  );
  const summaries = clusterSeparators(
    [...whitespaceCandidates, ...inkCandidates],
    strictTolerance,
  )
    .map(summarizeCluster)
    .filter((summary) => summary !== null);
  const strictCenters = summaries.map(({ center }) => center);
  for (const cluster of clusterSeparators(
    whitespaceCandidates,
    looseTolerance,
  )) {
    const summary = summarizeCluster(cluster);
    if (summary === null) continue;
    if (
      strictCenters.some(
        (strictCenter) =>
          Math.abs(summary.center - strictCenter) <= looseTolerance,
      )
    ) {
      continue;
    }
    summaries.push(summary);
  }
  if (summaries.length === 0) return [];

  const crossesWideComponent = (cut: number, backedByLinePair: boolean) =>
    !backedByLinePair &&
    features.components.some((component) => {
      const [left, componentTop, right, componentBottom] = component.bbox;
      const overlap =
        Math.min(bottom, componentBottom) - Math.max(top, componentTop);
      return (
        overlap >= (bottom - top) * 0.1 &&
        component.fillRatio > 0.08 &&
        left < cut &&
        cut < right &&
        right - left >= features.width * 0.3
      );
    });
  const horizontalRuleCount = features.separators.filter(
    (separator) =>
      separator.axis === "y" &&
      separator.spanEnd > 0 &&
      separator.spanStart < features.width &&
      separator.spanEnd - separator.spanStart >= features.width * 0.5,
  ).length;
  const tableRowEvidence =
    bottom - top >= 160 &&
    features.foregroundRatio >= 0.08 &&
    horizontalRuleCount >= 1;
  const whitespaceSummaryCount = summaries.filter(
    ({ backedByLinePair }) => !backedByLinePair,
  ).length;
  const structuralCutCount = summaries.filter(
    ({ backedByLinePair }) => backedByLinePair,
  ).length;
  const supportsVerticalSplit = (
    backedByLinePair: boolean,
    hasInkSupport: boolean,
  ) => {
    if (backedByLinePair) {
      return bottom - top >= 160 && features.foregroundRatio >= 0.08;
    }
    if (bottom - top < 160) return false;
    if (hasInkSupport) return features.foregroundRatio >= 0.08;
    if (tableRowEvidence) return true;
    if (structuralCutCount >= 2) return true;
    return features.foregroundRatio >= 0.08 && whitespaceSummaryCount >= 2;
  };
  const cuts = summaries
    .filter(
      ({ center, backedByLinePair, hasInkSupport }) =>
        supportsVerticalSplit(backedByLinePair, hasInkSupport) &&
        !crossesWideComponent(center, backedByLinePair),
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
  const horizontalBands = booleanParameter(
    stage,
    "mediumPageSegmentation",
    false,
  )
    ? mediumHorizontalBands(features, minRegionHeight, maxRegionHeight)
    : boundedHorizontalBands(features, minRegionHeight, maxRegionHeight);

  for (const [top, bottom] of horizontalBands) {
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

  const readingOrder = (regions: SourceRegion[]) =>
    [...regions].sort(
      (left, right) =>
        left.sourceY - right.sourceY ||
        left.sourceX - right.sourceX ||
        left.sourceHeight - right.sourceHeight ||
        left.sourceWidth - right.sourceWidth,
    );

  const stage = decision.stages[0];
  if (stage.name === "spatial_regions") {
    const regions = spatialRegions(features, stage);
    return regions.length > 0 ? readingOrder(regions) : [wholeImage];
  }
  if (stage.name === "table_regions") {
    return [wholeImage];
  }
  throw new Error(`Unknown browser layout stage '${stage.name}'.`);
}
