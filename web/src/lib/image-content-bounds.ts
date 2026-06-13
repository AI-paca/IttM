export interface PixelBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export function findContentBounds(
  data: Uint8ClampedArray,
  width: number,
  height: number,
  threshold = 240,
  padding = 20,
): PixelBounds {
  if (width <= 0 || height <= 0 || data.length < width * height * 4) {
    return { left: 0, top: 0, right: width, bottom: height };
  }

  let minX = width;
  let minY = height;
  let maxX = -1;
  let maxY = -1;

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4;
      if (
        data[offset] < threshold ||
        data[offset + 1] < threshold ||
        data[offset + 2] < threshold
      ) {
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
      }
    }
  }

  if (maxX < minX || maxY < minY) {
    return { left: 0, top: 0, right: width, bottom: height };
  }

  return {
    left: Math.max(0, minX - padding),
    top: Math.max(0, minY - padding),
    right: Math.min(width, maxX + 1 + padding),
    bottom: Math.min(height, maxY + 1 + padding),
  };
}
