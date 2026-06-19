export interface DenseGridCrop {
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
  targetWidth: number;
  targetHeight: number;
  pageSegmentationMode: string;
}

const TARGET_WIDTH = 3300;

export function overlappingStarts(
  limit: number,
  size: number,
  overlap: number,
): number[] {
  size = Math.max(1, Math.min(limit, size));
  overlap = Math.max(0, Math.min(size - 1, overlap));
  if (limit <= size) return [0];

  const starts: number[] = [];
  const step = size - overlap;
  for (let start = 0; start <= limit - size; start += step) {
    starts.push(start);
  }
  const last = limit - size;
  if (starts.at(-1) !== last) starts.push(last);
  return starts;
}

function groupedLineCount(indexes: readonly number[]): number {
  if (indexes.length === 0) return 0;
  let groups = 1;
  for (let index = 1; index < indexes.length; index += 1) {
    if (indexes[index] - indexes[index - 1] > 3) groups += 1;
  }
  return groups;
}

function isDark(
  rgba: Uint8ClampedArray,
  width: number,
  x: number,
  y: number,
): boolean {
  const offset = (y * width + x) * 4;
  return (
    rgba[offset] * 0.299 + rgba[offset + 1] * 0.587 + rgba[offset + 2] * 0.114 <
    210
  );
}

export function denseGridLineMetrics(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
): {
  horizontalLines: number;
  verticalLines: number;
  foregroundRatio: number;
} {
  const horizontal: number[] = [];
  const vertical: number[] = [];
  let foreground = 0;

  for (let y = 0; y < height; y += 1) {
    let longest = 0;
    let run = 0;
    for (let x = 0; x < width; x += 1) {
      if (isDark(rgba, width, x, y)) {
        foreground += 1;
        run += 1;
        longest = Math.max(longest, run);
      } else {
        run = 0;
      }
    }
    if (longest >= width * 0.3) horizontal.push(y);
  }

  for (let x = 0; x < width; x += 1) {
    let longest = 0;
    let run = 0;
    for (let y = 0; y < height; y += 1) {
      if (isDark(rgba, width, x, y)) {
        run += 1;
        longest = Math.max(longest, run);
      } else {
        run = 0;
      }
    }
    if (longest >= height * 0.3) vertical.push(x);
  }

  return {
    horizontalLines: groupedLineCount(horizontal),
    verticalLines: groupedLineCount(vertical),
    foregroundRatio: foreground / Math.max(1, width * height),
  };
}

export function looksLikeDenseGridPixels(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
): boolean {
  if (width < 600 || height < 400 || height / Math.max(1, width) > 1.6) {
    return false;
  }
  const { horizontalLines, verticalLines, foregroundRatio } =
    denseGridLineMetrics(rgba, width, height);
  return (
    horizontalLines >= 8 &&
    (verticalLines >= 8 || foregroundRatio <= 0.08 || horizontalLines >= 30)
  );
}

export function looksLikeSparseCoverPixels(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
): boolean {
  if (width < 600 || height < 400 || height / Math.max(1, width) > 0.9) {
    return false;
  }
  return denseGridLineMetrics(rgba, width, height).foregroundRatio <= 0.05;
}

export function denseGridLineIndexes(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
): { rows: Set<number>; columns: Set<number> } {
  const rows = new Set<number>();
  const columns = new Set<number>();

  for (let y = 0; y < height; y += 1) {
    let longest = 0;
    let run = 0;
    for (let x = 0; x < width; x += 1) {
      if (isDark(rgba, width, x, y)) {
        run += 1;
        longest = Math.max(longest, run);
      } else {
        run = 0;
      }
    }
    if (longest >= width * 0.25) {
      for (let offset = -2; offset <= 2; offset += 1) {
        if (y + offset >= 0 && y + offset < height) rows.add(y + offset);
      }
    }
  }

  for (let x = 0; x < width; x += 1) {
    let longest = 0;
    let run = 0;
    for (let y = 0; y < height; y += 1) {
      if (isDark(rgba, width, x, y)) {
        run += 1;
        longest = Math.max(longest, run);
      } else {
        run = 0;
      }
    }
    if (longest >= height * 0.25) {
      for (let offset = -2; offset <= 2; offset += 1) {
        if (x + offset >= 0 && x + offset < width) columns.add(x + offset);
      }
    }
  }
  return { rows, columns };
}

export function planDenseGridCrops(
  sourceWidth: number,
  sourceHeight: number,
  targetWidth = TARGET_WIDTH,
): DenseGridCrop[] {
  const workingWidth = Math.max(sourceWidth, targetWidth);
  const workingHeight = Math.max(
    1,
    Math.round((sourceHeight * workingWidth) / sourceWidth),
  );
  const sourceScale = sourceWidth / workingWidth;
  const crops: DenseGridCrop[] = [];

  const add = (
    left: number,
    top: number,
    right: number,
    bottom: number,
    scale: number,
    pageSegmentationMode: string,
  ) => {
    const sourceX = Math.max(0, Math.floor(left * sourceScale));
    const sourceY = Math.max(0, Math.floor(top * sourceScale));
    const sourceRight = Math.min(sourceWidth, Math.ceil(right * sourceScale));
    const sourceBottom = Math.min(
      sourceHeight,
      Math.ceil(bottom * sourceScale),
    );
    crops.push({
      sourceX,
      sourceY,
      sourceWidth: Math.max(1, sourceRight - sourceX),
      sourceHeight: Math.max(1, sourceBottom - sourceY),
      targetWidth: Math.max(1, Math.round((right - left) * scale)),
      targetHeight: Math.max(1, Math.round((bottom - top) * scale)),
      pageSegmentationMode,
    });
  };

  for (const [columns, rows, scale, psm] of [
    [6, 5, 3, "11"],
    [5, 4, 2, "6"],
  ] as const) {
    const tileWidth = Math.ceil(workingWidth / columns);
    const tileHeight = Math.ceil(workingHeight / rows);
    for (const top of overlappingStarts(
      workingHeight,
      tileHeight,
      Math.max(24, Math.floor(tileHeight / 12)),
    )) {
      for (const left of overlappingStarts(
        workingWidth,
        tileWidth,
        Math.max(24, Math.floor(tileWidth / 12)),
      )) {
        add(
          left,
          top,
          Math.min(workingWidth, left + tileWidth),
          Math.min(workingHeight, top + tileHeight),
          scale,
          psm,
        );
      }
    }
  }

  for (const [leftRatio, rightRatio, bandRatio, overlapRatio, scale, psm] of [
    [0, 0.38, 360 / 3300, 60 / 3300, 4, "6"],
    [0, 0.38, 360 / 3300, 60 / 3300, 4, "11"],
    [85 / 3300, 390 / 3300, 360 / 3300, 60 / 3300, 5, "6"],
    [85 / 3300, 390 / 3300, 360 / 3300, 60 / 3300, 5, "11"],
    [85 / 3300, 180 / 3300, 360 / 3300, 60 / 3300, 6, "6"],
    [130 / 3300, 390 / 3300, 100 / 3300, 20 / 3300, 6, "6"],
    [0, 0.38, 120 / 3300, 20 / 3300, 5, "6"],
  ] as const) {
    const left = Math.round(workingWidth * leftRatio);
    const right = Math.round(workingWidth * rightRatio);
    const bandHeight = Math.max(24, Math.round(workingWidth * bandRatio));
    const overlap = Math.round(workingWidth * overlapRatio);
    for (const top of overlappingStarts(workingHeight, bandHeight, overlap)) {
      add(
        left,
        top,
        right,
        Math.min(workingHeight, top + bandHeight),
        scale,
        psm,
      );
    }
  }

  const headerHeight = Math.max(1, Math.round((workingWidth * 220) / 3300));
  for (const [leftRatio, rightRatio, scale, psm] of [
    [0, 1200 / 3300, 4, "6"],
    [0, 1200 / 3300, 4, "11"],
    [0, 2200 / 3300, 3, "6"],
    [0, 2200 / 3300, 3, "11"],
    [2600 / 3300, 1, 4, "6"],
    [2600 / 3300, 1, 4, "11"],
  ] as const) {
    add(
      Math.round(workingWidth * leftRatio),
      0,
      Math.round(workingWidth * rightRatio),
      headerHeight,
      scale,
      psm,
    );
  }
  return crops;
}
