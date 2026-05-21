import type { BrowserOcrProfile } from "./browser-profile";
import { dewarpProjectedDocumentCanvas } from "./projected-document-dewarp";

interface ImageSize {
  width: number;
  height: number;
}

interface ResizeWorkerResponse {
  ok: boolean;
  blob?: Blob | null;
}

type BrowserCanvas = OffscreenCanvas | HTMLCanvasElement;

function targetSize(width: number, height: number, profile: BrowserOcrProfile) {
  const pixels = width * height;
  const dimensionScale = Math.min(
    1,
    profile.maxDimension / Math.max(width, height),
  );
  const pixelScale = Math.min(
    1,
    Math.sqrt(profile.maxImagePixels / Math.max(pixels, 1)),
  );
  const scale = Math.min(dimensionScale, pixelScale);

  if (scale >= 0.999) return null;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
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

async function resizeInWorker(
  file: File,
  profile: BrowserOcrProfile,
): Promise<File | Blob | null> {
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
    return await new Promise<File | Blob | null>((resolve) => {
      worker.onmessage = (event: MessageEvent<ResizeWorkerResponse>) => {
        const response = event.data;
        resolve(response.ok ? response.blob || file : null);
      };
      worker.onerror = () => resolve(null);
      worker.postMessage({
        file,
        maxImagePixels: profile.maxImagePixels,
        maxDimension: profile.maxDimension,
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

async function renderWithOffscreenCanvas(
  image: CanvasImageSource,
  size: ImageSize,
): Promise<BrowserCanvas | null> {
  if (typeof OffscreenCanvas === "undefined") return null;

  const canvas = new OffscreenCanvas(size.width, size.height);
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(image, 0, 0, size.width, size.height);
  return canvas;
}

async function renderWithHtmlCanvas(
  image: CanvasImageSource,
  size: ImageSize,
): Promise<BrowserCanvas | null> {
  if (typeof document === "undefined") return null;

  await yieldToBrowser();
  const canvas = document.createElement("canvas");
  canvas.width = size.width;
  canvas.height = size.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;

  ctx.drawImage(image, 0, 0, size.width, size.height);
  await yieldToBrowser();
  return canvas;
}

async function canvasToJpegBlob(canvas: BrowserCanvas): Promise<Blob | null> {
  if ("convertToBlob" in canvas) {
    return await canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
  }
  return await new Promise<Blob | null>((resolve) => {
    canvas.toBlob(resolve, "image/jpeg", 0.92);
  });
}

async function renderImageToCanvas(
  image: CanvasImageSource,
  size: ImageSize,
): Promise<BrowserCanvas | null> {
  return (
    (await renderWithOffscreenCanvas(image, size)) ||
    (await renderWithHtmlCanvas(image, size))
  );
}

async function preprocessInBrowserCanvas(
  file: File,
  profile: BrowserOcrProfile,
): Promise<File | Blob | null> {
  const image = await loadBrowserImage(file);
  if (!image) return null;

  try {
    const originalSize = { width: image.width, height: image.height };
    let canvas = await renderImageToCanvas(image, originalSize);
    if (!canvas) return null;

    if (profile.imagePreprocessing.includes("projected_document_dewarp")) {
      canvas =
        dewarpProjectedDocumentCanvas(
          canvas,
          profile.maxImagePixels,
          profile.maxDimension,
        ) || canvas;
    }

    if (profile.imagePreprocessing.includes("browser_resize")) {
      const size = targetSize(canvas.width, canvas.height, profile);
      if (size) {
        canvas = (await renderImageToCanvas(canvas, size)) || canvas;
      }
    }

    return (await canvasToJpegBlob(canvas)) || file;
  } finally {
    if ("close" in image && typeof image.close === "function") {
      image.close();
    }
  }
}

export async function resizeImageForBrowserOcr(
  file: File,
  profile: BrowserOcrProfile,
): Promise<File | Blob> {
  if (!file.type.startsWith("image/") || typeof document === "undefined") {
    return file;
  }

  if (!profile.imagePreprocessing.includes("projected_document_dewarp")) {
    const workerResult = await resizeInWorker(file, profile);
    if (workerResult) return workerResult;
  }

  return (await preprocessInBrowserCanvas(file, profile)) || file;
}
