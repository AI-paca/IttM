import type {
  ComponentFeature,
  LayoutFeatures,
  SeparatorCandidate,
} from "./layout-contracts";

export interface LayoutAnalysisRaster {
  data: Uint8ClampedArray;
  width: number;
  height: number;
  sourceWidth: number;
  sourceHeight: number;
}

const MAX_COMPONENTS = 20_000;

function groupedRanges(mask: ArrayLike<boolean>): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  let start = -1;
  for (let index = 0; index <= mask.length; index += 1) {
    if (index < mask.length && mask[index]) {
      if (start < 0) start = index;
      continue;
    }
    if (start >= 0) {
      ranges.push([start, index]);
      start = -1;
    }
  }
  return ranges;
}

function quantile(values: Float64Array, ratio: number): number {
  if (values.length === 0) return 0;
  const sorted = Array.from(values).sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.max(0, Math.floor((sorted.length - 1) * ratio)),
  );
  return sorted[index];
}

function otsuThreshold(gray: Uint8Array): number {
  const histogram = new Uint32Array(256);
  for (const value of gray) histogram[value] += 1;

  let weightedTotal = 0;
  for (let value = 0; value < histogram.length; value += 1) {
    weightedTotal += value * histogram[value];
  }

  let backgroundWeight = 0;
  let backgroundTotal = 0;
  let bestVariance = -1;
  let threshold = 127;
  for (let value = 0; value < histogram.length; value += 1) {
    backgroundWeight += histogram[value];
    if (backgroundWeight === 0) continue;
    const foregroundWeight = gray.length - backgroundWeight;
    if (foregroundWeight === 0) break;

    backgroundTotal += value * histogram[value];
    const backgroundMean = backgroundTotal / backgroundWeight;
    const foregroundMean = (weightedTotal - backgroundTotal) / foregroundWeight;
    const variance =
      backgroundWeight *
      foregroundWeight *
      (backgroundMean - foregroundMean) ** 2;
    if (variance > bestVariance) {
      bestVariance = variance;
      threshold = value;
    }
  }
  return threshold;
}

function foregroundMask(raster: LayoutAnalysisRaster): Uint8Array {
  const pixelCount = raster.width * raster.height;
  if (raster.data.length !== pixelCount * 4) {
    throw new RangeError("Layout raster has an invalid RGBA buffer length.");
  }

  const gray = new Uint8Array(pixelCount);
  for (let index = 0; index < pixelCount; index += 1) {
    const offset = index * 4;
    const alpha = raster.data[offset + 3] / 255;
    gray[index] = Math.round(
      255 * (1 - alpha) +
        alpha *
          (raster.data[offset] * 0.299 +
            raster.data[offset + 1] * 0.587 +
            raster.data[offset + 2] * 0.114),
    );
  }

  const threshold = otsuThreshold(gray);
  const mask = new Uint8Array(pixelCount);
  for (let index = 0; index < pixelCount; index += 1) {
    mask[index] = gray[index] <= threshold && gray[index] < 245 ? 1 : 0;
  }
  return mask;
}

function scaledInterval(
  start: number,
  end: number,
  scale: number,
  limit: number,
): [number, number] {
  return [
    Math.max(0, Math.min(limit, Math.round(start * scale))),
    Math.max(0, Math.min(limit, Math.round(end * scale))),
  ];
}

function rowDensities(
  mask: Uint8Array,
  width: number,
  height: number,
): Float64Array {
  const densities = new Float64Array(height);
  for (let y = 0; y < height; y += 1) {
    let foreground = 0;
    const rowOffset = y * width;
    for (let x = 0; x < width; x += 1) {
      foreground += mask[rowOffset + x];
    }
    densities[y] = foreground / width;
  }
  return densities;
}

function columnDensities(
  mask: Uint8Array,
  width: number,
  top: number,
  bottom: number,
): Float64Array {
  const densities = new Float64Array(width);
  const height = Math.max(1, bottom - top);
  for (let y = top; y < bottom; y += 1) {
    const rowOffset = y * width;
    for (let x = 0; x < width; x += 1) {
      densities[x] += mask[rowOffset + x];
    }
  }
  for (let x = 0; x < width; x += 1) densities[x] /= height;
  return densities;
}

function whitespaceCandidates(
  mask: Uint8Array,
  raster: LayoutAnalysisRaster,
): SeparatorCandidate[] {
  const { width, height, sourceWidth, sourceHeight } = raster;
  const xScale = sourceWidth / width;
  const yScale = sourceHeight / height;
  const candidates: SeparatorCandidate[] = [];

  const rows = rowDensities(mask, width, height);
  const rowThreshold = Math.max(0.001, Math.min(0.01, quantile(rows, 0.2)));
  const rowMask = Array.from(rows, (density) => density <= rowThreshold);
  const minRowGap = Math.max(2, Math.round(height * 0.0007));
  for (const [start, end] of groupedRanges(rowMask)) {
    if (end - start < minRowGap) continue;
    const [y1, y2] = scaledInterval(start, end, yScale, sourceHeight);
    let density = 0;
    for (let y = start; y < end; y += 1) density += rows[y];
    density /= Math.max(1, end - start);
    candidates.push({
      axis: "y",
      start: y1,
      end: y2,
      spanStart: 0,
      spanEnd: sourceWidth,
      kind: "whitespace",
      strength: Math.max(
        0,
        Math.min(1, 1 - density / Math.max(rowThreshold, 1e-6)),
      ),
    });
  }

  const windowHeights = Array.from(
    new Set(
      [0.5, 1, 1.75].map((ratio) =>
        Math.min(height, Math.max(40, Math.round(width * ratio))),
      ),
    ),
  ).sort((left, right) => left - right);
  const minColumnGap = Math.max(4, Math.round(width * 0.012));
  const edgeMargin = Math.max(2, Math.round(width * 0.02));
  for (const windowHeight of windowHeights) {
    const step = Math.max(1, Math.floor(windowHeight / 2));
    for (let top = 0; top < height; top += step) {
      const bottom = Math.min(height, top + windowHeight);
      if (bottom - top < Math.max(20, Math.floor(windowHeight / 3))) break;
      const columns = columnDensities(mask, width, top, bottom);
      const threshold = Math.max(
        0.002,
        Math.min(0.035, quantile(columns, 0.18) * 1.5),
      );
      const columnMask = Array.from(columns, (density) => density <= threshold);
      for (const [start, end] of groupedRanges(columnMask)) {
        if (end - start < minColumnGap) continue;
        if (start < edgeMargin || end > width - edgeMargin) continue;
        const [x1, x2] = scaledInterval(start, end, xScale, sourceWidth);
        const [y1, y2] = scaledInterval(top, bottom, yScale, sourceHeight);
        let density = 0;
        for (let x = start; x < end; x += 1) density += columns[x];
        density /= Math.max(1, end - start);
        candidates.push({
          axis: "x",
          start: x1,
          end: x2,
          spanStart: y1,
          spanEnd: y2,
          kind: "whitespace",
          strength: Math.max(
            0,
            Math.min(1, 1 - density / Math.max(threshold, 1e-6)),
          ),
        });
      }
      if (bottom === height) break;
    }
  }
  return candidates;
}

function inkLineCandidates(
  mask: Uint8Array,
  raster: LayoutAnalysisRaster,
): SeparatorCandidate[] {
  const { width, height, sourceWidth, sourceHeight } = raster;
  const xScale = sourceWidth / width;
  const yScale = sourceHeight / height;
  const rows = rowDensities(mask, width, height);
  const columns = columnDensities(mask, width, 0, height);
  const candidates: SeparatorCandidate[] = [];

  for (const [start, end] of groupedRanges(
    Array.from(columns, (density) => density >= 0.75),
  )) {
    const [x1, x2] = scaledInterval(start, end, xScale, sourceWidth);
    candidates.push({
      axis: "x",
      start: x1,
      end: x2,
      spanStart: 0,
      spanEnd: sourceHeight,
      kind: "ink",
      strength: Math.max(...columns.slice(start, end)),
    });
  }
  for (const [start, end] of groupedRanges(
    Array.from(rows, (density) => density >= 0.75),
  )) {
    const [y1, y2] = scaledInterval(start, end, yScale, sourceHeight);
    candidates.push({
      axis: "y",
      start: y1,
      end: y2,
      spanStart: 0,
      spanEnd: sourceWidth,
      kind: "ink",
      strength: Math.max(...rows.slice(start, end)),
    });
  }
  return candidates;
}

function joinedMask(
  mask: Uint8Array,
  width: number,
  height: number,
): Uint8Array {
  const joined = new Uint8Array(mask.length);
  const radius = Math.max(1, Math.round(width * 0.003));
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (!mask[y * width + x]) continue;
      const left = Math.max(0, x - radius);
      const right = Math.min(width - 1, x + radius);
      const top = Math.max(0, y - 1);
      const bottom = Math.min(height - 1, y + 1);
      for (let joinedY = top; joinedY <= bottom; joinedY += 1) {
        joined.fill(1, joinedY * width + left, joinedY * width + right + 1);
      }
    }
  }
  return joined;
}

function componentFeatures(
  mask: Uint8Array,
  raster: LayoutAnalysisRaster,
): ComponentFeature[] {
  const { width, height, sourceWidth, sourceHeight } = raster;
  const xScale = sourceWidth / width;
  const yScale = sourceHeight / height;
  const joined = joinedMask(mask, width, height);
  const visited = new Uint8Array(joined.length);
  const components: ComponentFeature[] = [];

  for (let seed = 0; seed < joined.length; seed += 1) {
    if (!joined[seed] || visited[seed]) continue;
    const stack = [seed];
    visited[seed] = 1;
    let minX = seed % width;
    let maxX = minX;
    let minY = Math.floor(seed / width);
    let maxY = minY;
    let area = 0;

    while (stack.length > 0) {
      const index = stack.pop()!;
      const x = index % width;
      const y = Math.floor(index / width);
      area += 1;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);

      for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
        for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
          if (offsetX === 0 && offsetY === 0) continue;
          const nextX = x + offsetX;
          const nextY = y + offsetY;
          if (nextX < 0 || nextY < 0 || nextX >= width || nextY >= height) {
            continue;
          }
          const next = nextY * width + nextX;
          if (!joined[next] || visited[next]) continue;
          visited[next] = 1;
          stack.push(next);
        }
      }
    }

    if (area < 4) continue;
    const [left, right] = scaledInterval(minX, maxX + 1, xScale, sourceWidth);
    const [top, bottom] = scaledInterval(minY, maxY + 1, yScale, sourceHeight);
    components.push({
      bbox: [left, top, right, bottom],
      area: Math.max(1, Math.round(area * xScale * yScale)),
      fillRatio: Math.min(
        1,
        area / Math.max(1, (maxX - minX + 1) * (maxY - minY + 1)),
      ),
    });
  }

  components.sort(
    (left, right) =>
      right.area - left.area ||
      left.bbox[1] - right.bbox[1] ||
      left.bbox[0] - right.bbox[0],
  );
  return components.slice(0, MAX_COMPONENTS);
}

function projectionGeometry(raster: LayoutAnalysisRaster): LayoutFeatures {
  const mask = foregroundMask(raster);
  let foreground = 0;
  for (const value of mask) foreground += value;
  const components = componentFeatures(mask, raster);

  return {
    width: raster.sourceWidth,
    height: raster.sourceHeight,
    foregroundRatio: foreground / Math.max(1, mask.length),
    separators: [
      ...whitespaceCandidates(mask, raster),
      ...inkLineCandidates(mask, raster),
    ],
    components,
    scalars: {
      analysisWidth: raster.width,
      analysisHeight: raster.height,
      aspectRatio: raster.sourceHeight / Math.max(1, raster.sourceWidth),
      componentCount: components.length,
      extractorAvailable: true,
    },
  };
}

const extractors: Readonly<
  Record<string, (raster: LayoutAnalysisRaster) => LayoutFeatures>
> = {
  projection_geometry: projectionGeometry,
};

export function collectBrowserLayoutFeatures(
  raster: LayoutAnalysisRaster,
  extractorNames: readonly string[],
): LayoutFeatures {
  if (extractorNames.length === 0) {
    return {
      width: raster.sourceWidth,
      height: raster.sourceHeight,
      foregroundRatio: 0,
      separators: [],
      components: [],
      scalars: {},
    };
  }

  const collected = extractorNames.map((name) => {
    const extractor = extractors[name];
    if (!extractor) {
      throw new Error(`Unknown browser layout feature extractor '${name}'.`);
    }
    return extractor(raster);
  });
  return {
    width: raster.sourceWidth,
    height: raster.sourceHeight,
    foregroundRatio: Math.max(
      ...collected.map((features) => features.foregroundRatio),
    ),
    separators: collected.flatMap((features) => features.separators),
    components: collected.flatMap((features) => features.components),
    scalars: Object.assign(
      {},
      ...collected.map((features) => features.scalars),
    ),
  };
}
