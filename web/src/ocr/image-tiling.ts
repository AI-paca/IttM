export interface ImageTile {
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
  targetWidth: number;
  targetHeight: number;
}

export interface ImageLimits {
  maxImagePixels: number;
  maxDimension: number;
}

export interface SourceImageRegion {
  sourceX: number;
  sourceY: number;
  sourceWidth: number;
  sourceHeight: number;
}

const LONG_IMAGE_ASPECT_RATIO = 4;
const TILE_OVERLAP_PIXELS = 128;

function boundedTargetSize(width: number, height: number, limits: ImageLimits) {
  const pixels = width * height;
  const dimensionScale = Math.min(
    1,
    limits.maxDimension / Math.max(width, height),
  );
  const pixelScale = Math.min(
    1,
    Math.sqrt(limits.maxImagePixels / Math.max(pixels, 1)),
  );
  const scale = Math.min(dimensionScale, pixelScale);

  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  };
}

export function planImageTiles(
  width: number,
  height: number,
  limits: ImageLimits,
): ImageTile[] {
  if (
    width <= 0 ||
    height <= 0 ||
    limits.maxImagePixels <= 0 ||
    limits.maxDimension <= 0
  ) {
    throw new RangeError("Image dimensions and OCR limits must be positive.");
  }

  if (height / width < LONG_IMAGE_ASPECT_RATIO) {
    const target = boundedTargetSize(width, height, limits);
    return [
      {
        sourceX: 0,
        sourceY: 0,
        sourceWidth: width,
        sourceHeight: height,
        targetWidth: target.width,
        targetHeight: target.height,
      },
    ];
  }

  const widthScale = Math.min(1, limits.maxDimension / width);
  const targetWidth = Math.max(1, Math.round(width * widthScale));
  const targetTileHeight = Math.max(
    1,
    Math.min(
      limits.maxDimension,
      Math.floor(limits.maxImagePixels / targetWidth),
    ),
  );
  const sourceTileHeight = Math.max(
    1,
    Math.floor(targetTileHeight / widthScale),
  );
  const sourceOverlap = Math.min(
    sourceTileHeight - 1,
    Math.max(1, Math.round(TILE_OVERLAP_PIXELS / widthScale)),
  );
  const sourceStep = Math.max(1, sourceTileHeight - sourceOverlap);
  const tiles: ImageTile[] = [];

  for (let sourceY = 0; sourceY < height; sourceY += sourceStep) {
    const sourceHeight = Math.min(sourceTileHeight, height - sourceY);
    tiles.push({
      sourceX: 0,
      sourceY,
      sourceWidth: width,
      sourceHeight,
      targetWidth,
      targetHeight: Math.max(1, Math.round(sourceHeight * widthScale)),
    });
    if (sourceY + sourceHeight >= height) break;
  }

  return tiles;
}

export function planRegionTiles(
  regions: readonly SourceImageRegion[],
  limits: ImageLimits,
): ImageTile[] {
  return regions.map((region) => {
    if (region.sourceWidth <= 0 || region.sourceHeight <= 0) {
      throw new RangeError("OCR source regions must have positive dimensions.");
    }
    const target = boundedTargetSize(
      region.sourceWidth,
      region.sourceHeight,
      limits,
    );
    return {
      ...region,
      targetWidth: target.width,
      targetHeight: target.height,
    };
  });
}
