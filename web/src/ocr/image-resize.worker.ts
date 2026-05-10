interface ResizeRequest {
  file: File;
  maxImagePixels: number;
  maxDimension: number;
}

interface ResizeSuccess {
  ok: true;
  blob: Blob | null;
}

interface ResizeFailure {
  ok: false;
  error: string;
}

function targetSize(width: number, height: number, request: ResizeRequest) {
  const pixels = width * height;
  const dimensionScale = Math.min(
    1,
    request.maxDimension / Math.max(width, height),
  );
  const pixelScale = Math.min(
    1,
    Math.sqrt(request.maxImagePixels / Math.max(pixels, 1)),
  );
  const scale = Math.min(dimensionScale, pixelScale);

  if (scale >= 0.999) return null;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

self.onmessage = async (event: MessageEvent<ResizeRequest>) => {
  try {
    const request = event.data;
    const bitmap = await createImageBitmap(request.file);
    const size = targetSize(bitmap.width, bitmap.height, request);

    if (!size) {
      bitmap.close();
      self.postMessage({ ok: true, blob: null } satisfies ResizeSuccess);
      return;
    }

    const canvas = new OffscreenCanvas(size.width, size.height);
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      bitmap.close();
      self.postMessage({
        ok: false,
        error: "Could not create OffscreenCanvas context",
      } satisfies ResizeFailure);
      return;
    }

    ctx.drawImage(bitmap, 0, 0, size.width, size.height);
    bitmap.close();
    const blob = await canvas.convertToBlob({
      type: "image/jpeg",
      quality: 0.92,
    });
    self.postMessage({ ok: true, blob } satisfies ResizeSuccess);
  } catch (error) {
    self.postMessage({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    } satisfies ResizeFailure);
  }
};

export {};
