import { findContentBounds } from "../lib/image-content-bounds";
import { readableStreamToBase64 } from "./base64-stream";

interface EncodeBase64Request {
  id: number;
  action: "encode-base64";
  blob: Blob;
}

interface PrepareImageRequest {
  id: number;
  action: "prepare-image";
  file: File;
  maxImagePixels: number;
  maxDimension: number;
  quality: number;
}

type EncodingRequest = EncodeBase64Request | PrepareImageRequest;

function targetSize(
  width: number,
  height: number,
  maxImagePixels: number,
  maxDimension: number,
) {
  const dimensionScale = Math.min(1, maxDimension / Math.max(width, height));
  const pixelScale = Math.min(
    1,
    Math.sqrt(maxImagePixels / Math.max(width * height, 1)),
  );
  const scale = Math.min(dimensionScale, pixelScale);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

async function prepareImage(request: PrepareImageRequest): Promise<Blob> {
  const bitmap = await createImageBitmap(request.file);
  try {
    const size = targetSize(
      bitmap.width,
      bitmap.height,
      request.maxImagePixels,
      request.maxDimension,
    );
    const canvas = new OffscreenCanvas(size.width, size.height);
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("Could not create OffscreenCanvas context.");
    context.drawImage(bitmap, 0, 0, size.width, size.height);

    const imageData = context.getImageData(0, 0, size.width, size.height);
    const bounds = findContentBounds(imageData.data, size.width, size.height);
    const cropWidth = Math.max(1, bounds.right - bounds.left);
    const cropHeight = Math.max(1, bounds.bottom - bounds.top);
    const output = new OffscreenCanvas(cropWidth, cropHeight);
    const outputContext = output.getContext("2d");
    if (!outputContext) throw new Error("Could not create output canvas.");
    outputContext.drawImage(
      canvas,
      bounds.left,
      bounds.top,
      cropWidth,
      cropHeight,
      0,
      0,
      cropWidth,
      cropHeight,
    );
    canvas.width = 1;
    canvas.height = 1;
    return await output.convertToBlob({
      type: "image/jpeg",
      quality: request.quality,
    });
  } finally {
    bitmap.close();
  }
}

self.onmessage = async (event: MessageEvent<EncodingRequest>) => {
  const request = event.data;
  try {
    const value =
      request.action === "encode-base64"
        ? await readableStreamToBase64(request.blob.stream())
        : await prepareImage(request);
    self.postMessage({ id: request.id, ok: true, value });
  } catch (error) {
    self.postMessage({
      id: request.id,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};

export {};
