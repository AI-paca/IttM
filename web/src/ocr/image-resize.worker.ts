import type { BrowserLayoutPipelineConfig } from "./layout-contracts";
import { planBrowserLayoutRegions } from "./layout-pipeline";
import {
  planImageTiles,
  planRegionTiles,
  type ImageTile,
} from "./image-tiling";

interface ResizeRequest {
  file: File;
  maxImagePixels: number;
  maxDimension: number;
  layout: BrowserLayoutPipelineConfig;
}

interface ResizeSuccess {
  ok: true;
  blobs: Blob[];
}

interface ResizeFailure {
  ok: false;
  error: string;
}

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
  request: ResizeRequest,
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

self.onmessage = async (event: MessageEvent<ResizeRequest>) => {
  let bitmap: ImageBitmap | undefined;
  try {
    const request = event.data;
    bitmap = await createImageBitmap(request.file);
    const tiles = planLayoutTiles(bitmap, request);

    if (
      tiles.length === 1 &&
      tiles[0].sourceX === 0 &&
      tiles[0].sourceY === 0 &&
      tiles[0].sourceWidth === bitmap.width &&
      tiles[0].sourceHeight === bitmap.height &&
      tiles[0].targetWidth === bitmap.width &&
      tiles[0].targetHeight === bitmap.height
    ) {
      self.postMessage({ ok: true, blobs: [] } satisfies ResizeSuccess);
      return;
    }

    const blobs: Blob[] = [];
    for (const tile of tiles) {
      const canvas = new OffscreenCanvas(tile.targetWidth, tile.targetHeight);
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        self.postMessage({
          ok: false,
          error: "Could not create OffscreenCanvas context",
        } satisfies ResizeFailure);
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
      blobs.push(
        await canvas.convertToBlob({
          type: "image/jpeg",
          quality: 0.92,
        }),
      );
    }

    self.postMessage({ ok: true, blobs } satisfies ResizeSuccess);
  } catch (error) {
    self.postMessage({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    } satisfies ResizeFailure);
  } finally {
    bitmap?.close();
  }
};

export {};
