import { planBrowserLayoutRegions } from "./layout-pipeline";
import type {
  ResizeWorkerCommand,
  ResizeWorkerRequest,
  ResizeWorkerResponse,
} from "./image-resize-protocol";
import {
  planImageTiles,
  planRegionTiles,
  type ImageTile,
} from "./image-tiling";

const MAX_ANALYSIS_PIXELS = 2_000_000;
const MAX_ANALYSIS_DIMENSION = 4096;

interface PlannedWorkerTile extends ImageTile {
  invert?: boolean;
}

function analysisSize(width: number, height: number) {
  const scale = Math.min(
    1,
    Math.sqrt(MAX_ANALYSIS_PIXELS / Math.max(1, width * height)),
    MAX_ANALYSIS_DIMENSION / Math.max(width, height),
  );
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function planLayoutTiles(
  bitmap: ImageBitmap,
  request: ResizeWorkerRequest,
): PlannedWorkerTile[] {
  if (request.layout.featureExtractors.length === 0) {
    return planImageTiles(bitmap.width, bitmap.height, request);
  }

  const size = analysisSize(bitmap.width, bitmap.height);
  const canvas = new OffscreenCanvas(size.width, size.height);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("Could not create layout analysis context");
  ctx.drawImage(bitmap, 0, 0, size.width, size.height);
  const imageData = ctx.getImageData(0, 0, size.width, size.height);
  const { regions } = planBrowserLayoutRegions(
    {
      data: imageData.data,
      width: size.width,
      height: size.height,
      sourceWidth: bitmap.width,
      sourceHeight: bitmap.height,
    },
    request.layout,
  );
  const tiles = planRegionTiles(regions, request);
  if (request.spatialFullPageFallback && tiles.length > 1) {
    tiles.push(planImageTiles(bitmap.width, bitmap.height, request)[0]);
  }
  return tiles;
}

function looksLikeDarkUiTextBitmap(bitmap: ImageBitmap): boolean {
  if (bitmap.width < 800 || bitmap.height < 400) return false;
  const size = analysisSize(bitmap.width, bitmap.height);
  const canvas = new OffscreenCanvas(size.width, size.height);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return false;
  ctx.drawImage(bitmap, 0, 0, size.width, size.height);
  const imageData = ctx.getImageData(0, 0, size.width, size.height);
  let dark = 0;
  let light = 0;
  const total = size.width * size.height;
  for (let index = 0; index < total; index += 1) {
    const offset = index * 4;
    const luminance =
      imageData.data[offset] * 0.299 +
      imageData.data[offset + 1] * 0.587 +
      imageData.data[offset + 2] * 0.114;
    if (luminance < 90) dark += 1;
    if (luminance > 190) light += 1;
  }
  return dark / Math.max(1, total) >= 0.65 && light / Math.max(1, total) >= 0.04;
}

function invertCanvas(canvas: OffscreenCanvas): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
  for (let index = 0; index < imageData.data.length; index += 4) {
    imageData.data[index] = 255 - imageData.data[index];
    imageData.data[index + 1] = 255 - imageData.data[index + 1];
    imageData.data[index + 2] = 255 - imageData.data[index + 2];
  }
  ctx.putImageData(imageData, 0, 0);
}

let releaseNextTile: (() => void) | undefined;

function waitForNextTile(): Promise<void> {
  return new Promise((resolve) => {
    releaseNextTile = resolve;
  });
}

async function processImage(request: ResizeWorkerRequest) {
  let bitmap: ImageBitmap | undefined;
  try {
    bitmap = await createImageBitmap(request.file);
    const tiles = planLayoutTiles(bitmap, request);
    if (request.darkUiTextFallback && looksLikeDarkUiTextBitmap(bitmap)) {
      tiles.push({
        ...planImageTiles(bitmap.width, bitmap.height, request)[0],
        invert: true,
      });
    }
    self.postMessage({
      type: "plan",
      total: tiles.length,
    } satisfies ResizeWorkerResponse);

    if (
      tiles.length === 1 &&
      tiles[0].sourceX === 0 &&
      tiles[0].sourceY === 0 &&
      tiles[0].sourceWidth === bitmap.width &&
      tiles[0].sourceHeight === bitmap.height &&
      tiles[0].targetWidth === bitmap.width &&
      tiles[0].targetHeight === bitmap.height &&
      request.ocrBorderPixels === 0
    ) {
      self.postMessage({
        type: "passthrough",
        total: 1,
        width: bitmap.width,
        height: bitmap.height,
      } satisfies ResizeWorkerResponse);
      self.postMessage({
        type: "complete",
        total: 1,
      } satisfies ResizeWorkerResponse);
      return;
    }

    for (const [index, tile] of tiles.entries()) {
      const outputWidth = tile.targetWidth + request.ocrBorderPixels * 2;
      const outputHeight = tile.targetHeight + request.ocrBorderPixels * 2;
      const canvas = new OffscreenCanvas(outputWidth, outputHeight);
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        self.postMessage({
          type: "error",
          error: "Could not create OffscreenCanvas context",
        } satisfies ResizeWorkerResponse);
        return;
      }

      ctx.fillStyle = "white";
      ctx.fillRect(0, 0, outputWidth, outputHeight);
      ctx.drawImage(
        bitmap,
        tile.sourceX,
        tile.sourceY,
        tile.sourceWidth,
        tile.sourceHeight,
        request.ocrBorderPixels,
        request.ocrBorderPixels,
        tile.targetWidth,
        tile.targetHeight,
      );
      if (tile.invert) invertCanvas(canvas);
      const blob = await canvas.convertToBlob({
        type: "image/jpeg",
        quality: 0.92,
      });
      self.postMessage({
        type: "tile",
        index,
        total: tiles.length,
        blob,
        width: outputWidth,
        height: outputHeight,
      } satisfies ResizeWorkerResponse);
      if (index < tiles.length - 1) await waitForNextTile();
    }

    self.postMessage({
      type: "complete",
      total: tiles.length,
    } satisfies ResizeWorkerResponse);
  } catch (error) {
    self.postMessage({
      type: "error",
      error: error instanceof Error ? error.message : String(error),
    } satisfies ResizeWorkerResponse);
  } finally {
    bitmap?.close();
  }
}

self.onmessage = (event: MessageEvent<ResizeWorkerCommand>) => {
  if (event.data.type === "next") {
    const release = releaseNextTile;
    releaseNextTile = undefined;
    release?.();
    return;
  }
  void processImage(event.data.request);
};

export {};
