import type { BrowserOcrProfile } from "./browser-profile";
import type {
  ResizeWorkerCommand,
  ResizeWorkerResponse,
} from "./image-resize-protocol";
import { dewarpProjectedDocumentCanvas } from "./projected-document-dewarp";
import { planImageTiles, type ImageTile } from "./image-tiling";

interface ImageSize {
  width: number;
  height: number;
}

export interface PreparedBrowserOcrInput {
  input: File | Blob;
  index: number;
  total: number;
}

type BrowserCanvas = OffscreenCanvas | HTMLCanvasElement;

export interface ResizeWorkerLike {
  onmessage: ((event: MessageEvent<ResizeWorkerResponse>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  postMessage(message: ResizeWorkerCommand): void;
  terminate(): void;
}

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

export function streamImagesFromResizeWorker(
  file: File,
  profile: BrowserOcrProfile,
  createWorker?: () => ResizeWorkerLike,
): AsyncIterable<PreparedBrowserOcrInput> | null {
  if (
    !createWorker &&
    (typeof Worker === "undefined" ||
      typeof createImageBitmap === "undefined" ||
      typeof OffscreenCanvas === "undefined")
  ) {
    return null;
  }

  return (async function* () {
    let worker: ResizeWorkerLike | undefined;
    const pending: ResizeWorkerResponse[] = [];
    let resolveNext: ((response: ResizeWorkerResponse) => void) | undefined;
    const push = (response: ResizeWorkerResponse) => {
      if (resolveNext) {
        const resolve = resolveNext;
        resolveNext = undefined;
        resolve(response);
      } else {
        pending.push(response);
      }
    };
    const next = () =>
      pending.length > 0
        ? Promise.resolve(pending.shift()!)
        : new Promise<ResizeWorkerResponse>((resolve) => {
            resolveNext = resolve;
          });

    try {
      worker = createWorker
        ? createWorker()
        : new Worker(new URL("./image-resize.worker.ts", import.meta.url), {
            type: "module",
          });
      worker.onmessage = (event) => push(event.data);
      worker.onerror = (event) =>
        push({
          type: "error",
          error: event.message || "Image resize worker failed",
        });
      worker.postMessage({
        type: "start",
        request: {
          file,
          maxImagePixels: profile.maxImagePixels,
          maxDimension: profile.maxDimension,
          layout: profile.layout,
        },
      });

      while (true) {
        const response = await next();
        if (response.type === "plan") continue;
        if (response.type === "passthrough") {
          yield { input: file, index: 0, total: 1 };
          continue;
        }
        if (response.type === "tile") {
          yield {
            input: response.blob,
            index: response.index,
            total: response.total,
          };
          worker.postMessage({ type: "next" });
          continue;
        }
        if (response.type === "complete") return;
        throw new Error(response.error);
      }
    } finally {
      worker?.terminate();
    }
  })();
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

async function* streamInBrowserCanvas(
  file: File,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const image = await loadBrowserImage(file);
  if (!image) return;

  try {
    const tiles = planImageTiles(image.width, image.height, profile);
    for (const [index, tile] of tiles.entries()) {
      let canvas = await renderTileToCanvas(image, tile);
      if (!canvas) throw new Error("Could not create browser OCR canvas");

      if (profile.imagePreprocessing.includes("projected_document_dewarp")) {
        canvas =
          dewarpProjectedDocumentCanvas(
            canvas,
            profile.maxImagePixels,
            profile.maxDimension,
          ) || canvas;
      }

      const blob = await canvasToJpegBlob(canvas);
      if (!blob) throw new Error("Could not encode browser OCR tile");
      yield { input: blob, index, total: tiles.length };
      await yieldToBrowser();
    }
  } finally {
    if ("close" in image && typeof image.close === "function") {
      image.close();
    }
  }
}

export async function* streamImagesForBrowserOcr(
  file: File,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  if (!file.type.startsWith("image/") || typeof document === "undefined") {
    yield { input: file, index: 0, total: 1 };
    return;
  }

  if (!profile.imagePreprocessing.includes("projected_document_dewarp")) {
    const workerStream = streamImagesFromResizeWorker(file, profile);
    if (workerStream) {
      let emitted = false;
      try {
        for await (const prepared of workerStream) {
          emitted = true;
          yield prepared;
        }
        return;
      } catch (error) {
        if (emitted) throw error;
      }
    }
  }

  let emitted = false;
  for await (const prepared of streamInBrowserCanvas(file, profile)) {
    emitted = true;
    yield prepared;
  }
  if (!emitted) yield { input: file, index: 0, total: 1 };
}
