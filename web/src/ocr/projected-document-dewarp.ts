interface Point {
  x: number;
  y: number;
}

interface CanvasLike {
  width: number;
  height: number;
}

interface ComponentSummary {
  area: number;
  boundary: Point[];
}

const DETECTION_MAX_DIMENSION = 420;
const THRESHOLDS = [180, 160, 140, 120];

function createCanvas(
  width: number,
  height: number,
): OffscreenCanvas | HTMLCanvasElement | null {
  if (typeof OffscreenCanvas !== "undefined") {
    return new OffscreenCanvas(width, height);
  }
  if (typeof document !== "undefined") {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    return canvas;
  }
  return null;
}

function canvasContext(canvas: OffscreenCanvas | HTMLCanvasElement) {
  return canvas.getContext("2d", { willReadFrequently: true });
}

function luminance(data: Uint8ClampedArray, offset: number): number {
  return (
    data[offset] * 0.299 + data[offset + 1] * 0.587 + data[offset + 2] * 0.114
  );
}

function thresholdMask(imageData: ImageData, threshold: number): Uint8Array {
  const { data, width, height } = imageData;
  const mask = new Uint8Array(width * height);
  for (let index = 0; index < width * height; index += 1) {
    if (luminance(data, index * 4) >= threshold) mask[index] = 1;
  }
  return mask;
}

function dilate(mask: Uint8Array, width: number, height: number): Uint8Array {
  const out = new Uint8Array(mask.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let value = 0;
      for (let dy = -1; dy <= 1 && !value; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const nx = x + dx;
          const ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          if (mask[ny * width + nx]) {
            value = 1;
            break;
          }
        }
      }
      out[y * width + x] = value;
    }
  }
  return out;
}

function erode(mask: Uint8Array, width: number, height: number): Uint8Array {
  const out = new Uint8Array(mask.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let value = 1;
      for (let dy = -1; dy <= 1 && value; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const nx = x + dx;
          const ny = y + dy;
          if (
            nx < 0 ||
            ny < 0 ||
            nx >= width ||
            ny >= height ||
            !mask[ny * width + nx]
          ) {
            value = 0;
            break;
          }
        }
      }
      out[y * width + x] = value;
    }
  }
  return out;
}

function closeMask(
  mask: Uint8Array,
  width: number,
  height: number,
): Uint8Array {
  return erode(
    erode(dilate(dilate(mask, width, height), width, height), width, height),
    width,
    height,
  );
}

function isBoundary(
  mask: Uint8Array,
  width: number,
  height: number,
  x: number,
  y: number,
): boolean {
  for (let dy = -1; dy <= 1; dy += 1) {
    for (let dx = -1; dx <= 1; dx += 1) {
      if (!dx && !dy) continue;
      const nx = x + dx;
      const ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= width || ny >= height) return true;
      if (!mask[ny * width + nx]) return true;
    }
  }
  return false;
}

function largestComponent(
  mask: Uint8Array,
  width: number,
  height: number,
): ComponentSummary | null {
  const visited = new Uint8Array(mask.length);
  let best: ComponentSummary | null = null;
  const stack: number[] = [];

  for (let start = 0; start < mask.length; start += 1) {
    if (!mask[start] || visited[start]) continue;

    let area = 0;
    const boundary: Point[] = [];
    visited[start] = 1;
    stack.push(start);

    while (stack.length) {
      const index = stack.pop() as number;
      const x = index % width;
      const y = Math.floor(index / width);
      area += 1;
      if (isBoundary(mask, width, height, x, y)) boundary.push({ x, y });

      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          if (!dx && !dy) continue;
          const nx = x + dx;
          const ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          const next = ny * width + nx;
          if (!mask[next] || visited[next]) continue;
          visited[next] = 1;
          stack.push(next);
        }
      }
    }

    if (!best || area > best.area) best = { area, boundary };
  }

  return best;
}

function cross(origin: Point, a: Point, b: Point): number {
  return (
    (a.x - origin.x) * (b.y - origin.y) - (a.y - origin.y) * (b.x - origin.x)
  );
}

function convexHull(points: Point[]): Point[] {
  const sorted = [...points].sort((a, b) =>
    a.x === b.x ? a.y - b.y : a.x - b.x,
  );
  if (sorted.length <= 1) return sorted;

  const lower: Point[] = [];
  for (const point of sorted) {
    while (
      lower.length >= 2 &&
      cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0
    ) {
      lower.pop();
    }
    lower.push(point);
  }

  const upper: Point[] = [];
  for (let index = sorted.length - 1; index >= 0; index -= 1) {
    const point = sorted[index];
    while (
      upper.length >= 2 &&
      cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0
    ) {
      upper.pop();
    }
    upper.push(point);
  }

  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

function extremeQuad(points: Point[]): [Point, Point, Point, Point] | null {
  if (points.length < 4) return null;

  const hull = convexHull(points);
  if (hull.length < 4) return null;

  const topLeft = hull.reduce(
    (best, point) => (point.x + point.y < best.x + best.y ? point : best),
    hull[0],
  );
  const topRight = hull.reduce(
    (best, point) => (point.x - point.y > best.x - best.y ? point : best),
    hull[0],
  );
  const bottomRight = hull.reduce(
    (best, point) => (point.x + point.y > best.x + best.y ? point : best),
    hull[0],
  );
  const bottomLeft = hull.reduce(
    (best, point) => (point.y - point.x > best.y - best.x ? point : best),
    hull[0],
  );
  return [topLeft, topRight, bottomRight, bottomLeft];
}

function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function isNearFullFrame(
  quad: [Point, Point, Point, Point],
  width: number,
  height: number,
  areaRatio: number,
): boolean {
  if (areaRatio < 0.85) return false;
  const tolerance = Math.max(8, Math.min(width, height) * 0.03);
  const expected = [
    { x: 0, y: 0 },
    { x: width - 1, y: 0 },
    { x: width - 1, y: height - 1 },
    { x: 0, y: height - 1 },
  ];
  return quad.every(
    (point, index) => distance(point, expected[index]) <= tolerance,
  );
}

function solveLinearSystem(matrix: number[][], vector: number[]): number[] {
  const n = vector.length;
  const augmented = matrix.map((row, index) => [...row, vector[index]]);

  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1) {
      if (Math.abs(augmented[row][col]) > Math.abs(augmented[pivot][col]))
        pivot = row;
    }
    [augmented[col], augmented[pivot]] = [augmented[pivot], augmented[col]];

    const divisor = augmented[col][col] || 1;
    for (let c = col; c <= n; c += 1) augmented[col][c] /= divisor;

    for (let row = 0; row < n; row += 1) {
      if (row === col) continue;
      const factor = augmented[row][col];
      for (let c = col; c <= n; c += 1)
        augmented[row][c] -= factor * augmented[col][c];
    }
  }

  return augmented.map((row) => row[n]);
}

function homography(
  from: [Point, Point, Point, Point],
  to: [Point, Point, Point, Point],
): number[] {
  const matrix: number[][] = [];
  const vector: number[] = [];

  for (let index = 0; index < 4; index += 1) {
    const src = from[index];
    const dst = to[index];
    matrix.push([src.x, src.y, 1, 0, 0, 0, -dst.x * src.x, -dst.x * src.y]);
    vector.push(dst.x);
    matrix.push([0, 0, 0, src.x, src.y, 1, -dst.y * src.x, -dst.y * src.y]);
    vector.push(dst.y);
  }

  const values = solveLinearSystem(matrix, vector);
  return [...values, 1];
}

function mapPoint(h: number[], x: number, y: number): Point {
  const denominator = h[6] * x + h[7] * y + h[8];
  return {
    x: (h[0] * x + h[1] * y + h[2]) / denominator,
    y: (h[3] * x + h[4] * y + h[5]) / denominator,
  };
}

function sampleBilinear(
  imageData: ImageData,
  x: number,
  y: number,
  channel: number,
): number {
  const { width, height, data } = imageData;
  const clampedX = Math.max(0, Math.min(width - 1, x));
  const clampedY = Math.max(0, Math.min(height - 1, y));
  const x0 = Math.floor(clampedX);
  const y0 = Math.floor(clampedY);
  const x1 = Math.min(width - 1, x0 + 1);
  const y1 = Math.min(height - 1, y0 + 1);
  const dx = clampedX - x0;
  const dy = clampedY - y0;

  const offset00 = (y0 * width + x0) * 4 + channel;
  const offset10 = (y0 * width + x1) * 4 + channel;
  const offset01 = (y1 * width + x0) * 4 + channel;
  const offset11 = (y1 * width + x1) * 4 + channel;
  return (
    data[offset00] * (1 - dx) * (1 - dy) +
    data[offset10] * dx * (1 - dy) +
    data[offset01] * (1 - dx) * dy +
    data[offset11] * dx * dy
  );
}

function detectQuad(imageData: ImageData): [Point, Point, Point, Point] | null {
  const { width, height } = imageData;
  for (const threshold of THRESHOLDS) {
    const mask = closeMask(thresholdMask(imageData, threshold), width, height);
    const component = largestComponent(mask, width, height);
    if (!component) continue;

    const areaRatio = component.area / Math.max(1, width * height);
    if (areaRatio < 0.18 || areaRatio > 0.92) continue;

    const quad = extremeQuad(component.boundary);
    if (!quad || isNearFullFrame(quad, width, height, areaRatio)) continue;
    return quad;
  }
  return null;
}

export function dewarpProjectedDocumentCanvas(
  source: OffscreenCanvas | HTMLCanvasElement,
  maxOutputPixels: number,
  maxOutputDimension: number,
): OffscreenCanvas | HTMLCanvasElement | null {
  const scale = Math.min(
    1,
    DETECTION_MAX_DIMENSION / Math.max(source.width, source.height),
  );
  const detectionWidth = Math.max(1, Math.round(source.width * scale));
  const detectionHeight = Math.max(1, Math.round(source.height * scale));
  const detectionCanvas = createCanvas(detectionWidth, detectionHeight);
  if (!detectionCanvas) return null;

  const detectionCtx = canvasContext(detectionCanvas);
  if (!detectionCtx) return null;
  detectionCtx.drawImage(source, 0, 0, detectionWidth, detectionHeight);

  const quad = detectQuad(
    detectionCtx.getImageData(0, 0, detectionWidth, detectionHeight),
  );
  if (!quad) return null;

  const sourceQuad = quad.map((point) => ({
    x: point.x / scale,
    y: point.y / scale,
  })) as [Point, Point, Point, Point];

  const targetWidthRaw = Math.round(
    (distance(sourceQuad[0], sourceQuad[1]) +
      distance(sourceQuad[3], sourceQuad[2])) /
      2,
  );
  const targetHeightRaw = Math.round(
    (distance(sourceQuad[0], sourceQuad[3]) +
      distance(sourceQuad[1], sourceQuad[2])) /
      2,
  );
  if (targetWidthRaw < 250 || targetHeightRaw < 180) return null;

  const outputScale = Math.min(
    1,
    maxOutputDimension / Math.max(targetWidthRaw, targetHeightRaw),
    Math.sqrt(maxOutputPixels / Math.max(1, targetWidthRaw * targetHeightRaw)),
  );
  const targetWidth = Math.max(1, Math.round(targetWidthRaw * outputScale));
  const targetHeight = Math.max(1, Math.round(targetHeightRaw * outputScale));
  const outputCanvas = createCanvas(targetWidth, targetHeight);
  if (!outputCanvas) return null;

  const sourceCtx = canvasContext(source);
  const outputCtx = canvasContext(outputCanvas);
  if (!sourceCtx || !outputCtx) return null;

  const sourceData = sourceCtx.getImageData(0, 0, source.width, source.height);
  const outputData = outputCtx.createImageData(targetWidth, targetHeight);
  const targetQuad: [Point, Point, Point, Point] = [
    { x: 0, y: 0 },
    { x: targetWidth - 1, y: 0 },
    { x: targetWidth - 1, y: targetHeight - 1 },
    { x: 0, y: targetHeight - 1 },
  ];
  const scaledSourceQuad = sourceQuad.map((point) => ({
    x: point.x,
    y: point.y,
  })) as [Point, Point, Point, Point];
  const h = homography(targetQuad, scaledSourceQuad);

  for (let y = 0; y < targetHeight; y += 1) {
    for (let x = 0; x < targetWidth; x += 1) {
      const mapped = mapPoint(h, x, y);
      const outOffset = (y * targetWidth + x) * 4;
      outputData.data[outOffset] = sampleBilinear(
        sourceData,
        mapped.x,
        mapped.y,
        0,
      );
      outputData.data[outOffset + 1] = sampleBilinear(
        sourceData,
        mapped.x,
        mapped.y,
        1,
      );
      outputData.data[outOffset + 2] = sampleBilinear(
        sourceData,
        mapped.x,
        mapped.y,
        2,
      );
      outputData.data[outOffset + 3] = 255;
    }
  }

  outputCtx.putImageData(outputData, 0, 0);
  return outputCanvas;
}
