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
): ImageTile[] {
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
  return planRegionTiles(regions, request);
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
      tiles[0].targetHeight === bitmap.height
    ) {
      self.postMessage({
        type: "passthrough",
        total: 1,
      } satisfies ResizeWorkerResponse);
      self.postMessage({
        type: "complete",
        total: 1,
      } satisfies ResizeWorkerResponse);
      return;
    }

    for (const [index, tile] of tiles.entries()) {
      const canvas = new OffscreenCanvas(tile.targetWidth, tile.targetHeight);
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        self.postMessage({
          type: "error",
          error: "Could not create OffscreenCanvas context",
        } satisfies ResizeWorkerResponse);
        return;
      }

      ctx.drawImage(
        bitmap,
        tile.sourceX,
        tile.sourceY,
        tile.sourceWidth,
        tile.sourceHeight,
        0,
        0,
        tile.targetWidth,
        tile.targetHeight,
      );
      const blob = await canvas.convertToBlob({
        type: "image/jpeg",
        quality: 0.92,
      });
      self.postMessage({
        type: "tile",
        index,
        total: tiles.length,
        blob,
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
