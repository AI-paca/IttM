import type { BrowserOcrProfile } from "./browser-profile";
import { dewarpProjectedDocumentCanvas } from "./projected-document-dewarp";
import { planImageTiles, type ImageTile } from "./image-tiling";

interface ImageSize {
  width: number;
  height: number;
}

interface ResizeWorkerResponse {
  ok: boolean;
  blobs?: Blob[];
}

type BrowserCanvas = OffscreenCanvas | HTMLCanvasElement;

function yieldToBrowser(): Promise<void> {
  const scheduler = (
    globalThis as unknown as {
      scheduler?: { postTask?: (callback: () => void) => Promise<void> };
    }
  ).scheduler;

  if (scheduler?.postTask) {
    return scheduler.postTask(() => undefined);
  }
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function resizeInWorker(
  file: File,
  profile: BrowserOcrProfile,
): Promise<Array<File | Blob> | null> {
  if (
    typeof Worker === "undefined" ||
    typeof createImageBitmap === "undefined" ||
    typeof OffscreenCanvas === "undefined"
  ) {
    return null;
  }

  const worker = new Worker(
    new URL("./image-resize.worker.ts", import.meta.url),
    {
      type: "module",
    },
  );

  try {
    return await new Promise<Array<File | Blob> | null>((resolve) => {
      worker.onmessage = (event: MessageEvent<ResizeWorkerResponse>) => {
        const response = event.data;
        resolve(
          response.ok
            ? response.blobs?.length
              ? response.blobs
              : [file]
            : null,
        );
      };
      worker.onerror = () => resolve(null);
      worker.postMessage({
        file,
        maxImagePixels: profile.maxImagePixels,
        maxDimension: profile.maxDimension,
        layout: profile.layout,
      });
    });
  } finally {
    worker.terminate();
  }
}

function loadImageElement(file: File): Promise<HTMLImageElement | null> {
  return new Promise((resolve) => {
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);

    img.onload = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(img);
    };

    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(null);
    };

    img.src = objectUrl;
  });
}

async function loadBrowserImage(
  file: File,
): Promise<(ImageBitmap & ImageSize) | HTMLImageElement | null> {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file);
    } catch {
      // Fall back to HTMLImageElement for older browsers and unusual image payloads.
    }
  }

  if (typeof document === "undefined") return null;
  return await loadImageElement(file);
}

async function canvasToJpegBlob(canvas: BrowserCanvas): Promise<Blob | null> {
  if ("convertToBlob" in canvas) {
    return await canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
  }
  return await new Promise<Blob | null>((resolve) => {
    canvas.toBlob(resolve, "image/jpeg", 0.92);
  });
}

async function renderTileToCanvas(
  image: CanvasImageSource,
  tile: ImageTile,
): Promise<BrowserCanvas | null> {
  const canvas =
    typeof OffscreenCanvas !== "undefined"
      ? new OffscreenCanvas(tile.targetWidth, tile.targetHeight)
      : typeof document !== "undefined"
        ? document.createElement("canvas")
        : null;
  if (!canvas) return null;

  canvas.width = tile.targetWidth;
  canvas.height = tile.targetHeight;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(
    image,
    tile.sourceX,
    tile.sourceY,
    tile.sourceWidth,
    tile.sourceHeight,
    0,
    0,
    tile.targetWidth,
    tile.targetHeight,
  );
  return canvas;
}

async function preprocessInBrowserCanvas(
  file: File,
  profile: BrowserOcrProfile,
): Promise<Array<File | Blob> | null> {
  const image = await loadBrowserImage(file);
  if (!image) return null;

  try {
    const tiles = planImageTiles(image.width, image.height, profile);
    const results: Blob[] = [];
    for (const tile of tiles) {
      let canvas = await renderTileToCanvas(image, tile);
      if (!canvas) return null;

      if (profile.imagePreprocessing.includes("projected_document_dewarp")) {
        canvas =
          dewarpProjectedDocumentCanvas(
            canvas,
            profile.maxImagePixels,
            profile.maxDimension,
          ) || canvas;
      }

      const blob = await canvasToJpegBlob(canvas);
      if (!blob) return null;
      results.push(blob);
      await yieldToBrowser();
    }

    return results;
  } finally {
    if ("close" in image && typeof image.close === "function") {
      image.close();
    }
  }
}

export async function prepareImagesForBrowserOcr(
  file: File,
  profile: BrowserOcrProfile,
): Promise<Array<File | Blob>> {
  if (!file.type.startsWith("image/") || typeof document === "undefined") {
    return [file];
  }

  if (!profile.imagePreprocessing.includes("projected_document_dewarp")) {
    const workerResult = await resizeInWorker(file, profile);
    if (workerResult) return workerResult;
  }

  return (await preprocessInBrowserCanvas(file, profile)) || [file];
}
