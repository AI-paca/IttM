import { cropWhiteBorders } from "../lib/pdf-parser";
import { readableStreamToBase64 } from "./base64-stream";

interface WorkerResponse<T> {
  id: number;
  ok: boolean;
  value?: T;
  error?: string;
}

export interface LlmImageLimits {
  maxImagePixels: number;
  maxDimension: number;
  quality: number;
}

export const DEFAULT_LLM_IMAGE_LIMITS: LlmImageLimits = {
  maxImagePixels: 12_000_000,
  maxDimension: 4096,
  quality: 0.9,
};

let nextRequestId = 0;
const ENCODING_WORKER_TIMEOUT_MS = 120_000;

function runEncodingWorker<T>(payload: Record<string, unknown>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const worker = new Worker(
      new URL("./document-encoding.worker.ts", import.meta.url),
      { type: "module" },
    );
    const id = ++nextRequestId;
    const timeout = setTimeout(() => {
      worker.terminate();
      reject(new Error("Document encoding worker timed out."));
    }, ENCODING_WORKER_TIMEOUT_MS);

    worker.onmessage = (event: MessageEvent<WorkerResponse<T>>) => {
      if (event.data.id !== id) return;
      clearTimeout(timeout);
      worker.terminate();
      if (event.data.ok && event.data.value !== undefined) {
        resolve(event.data.value);
      } else {
        reject(new Error(event.data.error || "Document encoding failed."));
      }
    };
    worker.onerror = (event) => {
      clearTimeout(timeout);
      worker.terminate();
      reject(new Error(event.message || "Document encoding worker failed."));
    };
    worker.postMessage({ id, ...payload });
  });
}

async function prepareImageFallback(
  file: File,
  limits: LlmImageLimits,
): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  try {
    const dimensionScale = Math.min(
      1,
      limits.maxDimension / Math.max(bitmap.width, bitmap.height),
    );
    const pixelScale = Math.min(
      1,
      Math.sqrt(
        limits.maxImagePixels / Math.max(bitmap.width * bitmap.height, 1),
      ),
    );
    const scale = Math.min(dimensionScale, pixelScale);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("Could not create image canvas.");
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const cropped = cropWhiteBorders(canvas);
    return await new Promise<Blob>((resolve, reject) => {
      cropped.toBlob(
        (blob) =>
          blob ? resolve(blob) : reject(new Error("Canvas returned no image.")),
        "image/jpeg",
        limits.quality,
      );
    });
  } finally {
    bitmap.close();
  }
}

export async function prepareImageForLlm(
  file: File,
  limits: LlmImageLimits = DEFAULT_LLM_IMAGE_LIMITS,
): Promise<Blob> {
  if (
    typeof Worker !== "undefined" &&
    typeof OffscreenCanvas !== "undefined" &&
    typeof createImageBitmap !== "undefined"
  ) {
    try {
      return await runEncodingWorker<Blob>({
        action: "prepare-image",
        file,
        ...limits,
      });
    } catch (error) {
      console.warn(
        "Document encoding worker unavailable, using fallback.",
        error,
      );
    }
  }
  return await prepareImageFallback(file, limits);
}

export async function blobToBase64OffMainThread(blob: Blob): Promise<string> {
  if (typeof Worker !== "undefined") {
    try {
      return await runEncodingWorker<string>({
        action: "encode-base64",
        blob,
      });
    } catch (error) {
      console.warn(
        "Base64 worker unavailable, using streaming fallback.",
        error,
      );
    }
  }
  return await readableStreamToBase64(blob.stream());
}
