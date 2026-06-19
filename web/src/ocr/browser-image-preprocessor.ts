import type { BrowserOcrProfile } from "./browser-profile";
import type {
  ResizeWorkerCommand,
  ResizeWorkerResponse,
} from "./image-resize-protocol";
import {
  dewarpProjectedDocumentCanvas,
  dewarpProjectorSlideCanvas,
} from "./projected-document-dewarp";
import {
  denseGridLineIndexes,
  looksLikeDenseGridPixels,
  looksLikeSparseCoverPixels,
  planDenseGridCrops,
} from "./browser-dense-grid";
import { planImageTiles, type ImageTile } from "./image-tiling";

interface ImageSize {
  width: number;
  height: number;
}

export interface PreparedBrowserOcrInput {
  input: File | Blob;
  index: number;
  total: number;
  pageSegmentationMode?: string;
}

type BrowserCanvas = OffscreenCanvas | HTMLCanvasElement;
const MAX_PROJECTED_DOCUMENT_ASPECT_RATIO = 2;

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
          ocrBorderPixels: profile.imagePreprocessing.includes("ocr_border")
            ? profile.ocrBorderPixels
            : 0,
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
  borderPixels = 0,
): Promise<BrowserCanvas | null> {
  const outputWidth = tile.targetWidth + borderPixels * 2;
  const outputHeight = tile.targetHeight + borderPixels * 2;
  const canvas =
    typeof OffscreenCanvas !== "undefined"
      ? new OffscreenCanvas(outputWidth, outputHeight)
      : typeof document !== "undefined"
        ? document.createElement("canvas")
        : null;
  if (!canvas) return null;

  canvas.width = outputWidth;
  canvas.height = outputHeight;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.fillStyle = "white";
  ctx.fillRect(0, 0, outputWidth, outputHeight);
  ctx.drawImage(
    image,
    tile.sourceX,
    tile.sourceY,
    tile.sourceWidth,
    tile.sourceHeight,
    borderPixels,
    borderPixels,
    tile.targetWidth,
    tile.targetHeight,
  );
  return canvas;
}

function eraseDenseGridLines(canvas: BrowserCanvas): void {
  const context = canvas.getContext("2d");
  if (!context) return;
  const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
  const { rows, columns } = denseGridLineIndexes(
    imageData.data,
    canvas.width,
    canvas.height,
  );
  for (const y of rows) {
    for (let x = 0; x < canvas.width; x += 1) {
      const offset = (y * canvas.width + x) * 4;
      imageData.data[offset] = 255;
      imageData.data[offset + 1] = 255;
      imageData.data[offset + 2] = 255;
      imageData.data[offset + 3] = 255;
    }
  }
  for (const x of columns) {
    for (let y = 0; y < canvas.height; y += 1) {
      const offset = (y * canvas.width + x) * 4;
      imageData.data[offset] = 255;
      imageData.data[offset + 1] = 255;
      imageData.data[offset + 2] = 255;
      imageData.data[offset + 3] = 255;
    }
  }
  context.putImageData(imageData, 0, 0);
}

async function looksLikeDenseGridImage(
  image: CanvasImageSource & ImageSize,
): Promise<boolean> {
  if (
    image.width < 1800 ||
    image.height < 1200 ||
    image.height / Math.max(1, image.width) > 1.6
  ) {
    return false;
  }
  const scale = Math.min(1, 1200 / image.width, 900 / image.height);
  const analysis = await renderTileToCanvas(image, {
    sourceX: 0,
    sourceY: 0,
    sourceWidth: image.width,
    sourceHeight: image.height,
    targetWidth: Math.max(1, Math.round(image.width * scale)),
    targetHeight: Math.max(1, Math.round(image.height * scale)),
  });
  if (!analysis) return false;
  const context = analysis.getContext("2d");
  if (!context) return false;
  const imageData = context.getImageData(0, 0, analysis.width, analysis.height);
  return looksLikeDenseGridPixels(
    imageData.data,
    analysis.width,
    analysis.height,
  );
}

async function looksLikeSparseCoverImage(
  image: CanvasImageSource & ImageSize,
): Promise<boolean> {
  if (
    image.width < 2200 ||
    image.height < 1500 ||
    image.height / Math.max(1, image.width) > 0.9
  ) {
    return false;
  }
  const scale = Math.min(1, 1200 / image.width, 900 / image.height);
  const analysis = await renderTileToCanvas(image, {
    sourceX: 0,
    sourceY: 0,
    sourceWidth: image.width,
    sourceHeight: image.height,
    targetWidth: Math.max(1, Math.round(image.width * scale)),
    targetHeight: Math.max(1, Math.round(image.height * scale)),
  });
  if (!analysis) return false;
  const context = analysis.getContext("2d");
  if (!context) return false;
  const imageData = context.getImageData(0, 0, analysis.width, analysis.height);
  return looksLikeSparseCoverPixels(
    imageData.data,
    analysis.width,
    analysis.height,
  );
}

function imageDataInkRatio(
  imageData: ImageData,
  left: number,
  top: number,
  width: number,
  height: number,
): number {
  let dark = 0;
  let total = 0;
  const right = Math.min(imageData.width, left + width);
  const bottom = Math.min(imageData.height, top + height);
  for (let y = Math.max(0, top); y < bottom; y += 1) {
    for (let x = Math.max(0, left); x < right; x += 1) {
      const offset = (y * imageData.width + x) * 4;
      const luminance =
        imageData.data[offset] * 0.299 +
        imageData.data[offset + 1] * 0.587 +
        imageData.data[offset + 2] * 0.114;
      if (luminance < 220) dark += 1;
      total += 1;
    }
  }
  return dark / Math.max(1, total);
}

async function looksLikeEdgeToEdgeWordImage(
  image: CanvasImageSource & ImageSize,
): Promise<boolean> {
  const aspect = image.height / Math.max(1, image.width);
  if (image.width < 1200 || image.height < 600 || aspect < 0.25 || aspect > 1) {
    return false;
  }
  const scale = Math.min(1, 1200 / image.width, 900 / image.height);
  const analysis = await renderTileToCanvas(image, {
    sourceX: 0,
    sourceY: 0,
    sourceWidth: image.width,
    sourceHeight: image.height,
    targetWidth: Math.max(1, Math.round(image.width * scale)),
    targetHeight: Math.max(1, Math.round(image.height * scale)),
  });
  if (!analysis) return false;
  const context = analysis.getContext("2d");
  if (!context) return false;
  const imageData = context.getImageData(0, 0, analysis.width, analysis.height);
  const edge = Math.max(
    2,
    Math.min(12, Math.floor(Math.min(analysis.width, analysis.height) / 80)),
  );
  const top = imageDataInkRatio(imageData, 0, 0, analysis.width, edge);
  const bottom = imageDataInkRatio(
    imageData,
    0,
    analysis.height - edge,
    analysis.width,
    edge,
  );
  const left = imageDataInkRatio(imageData, 0, 0, edge, analysis.height);
  const right = imageDataInkRatio(
    imageData,
    analysis.width - edge,
    0,
    edge,
    analysis.height,
  );
  const overall = imageDataInkRatio(
    imageData,
    0,
    0,
    analysis.width,
    analysis.height,
  );
  return (
    top >= 0.02 &&
    top <= 0.2 &&
    bottom >= 0.02 &&
    bottom <= 0.2 &&
    left >= 0.1 &&
    right >= 0.1 &&
    overall <= 0.8
  );
}

async function* streamDenseGridCanvas(
  image: CanvasImageSource & ImageSize,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const crops = planDenseGridCrops(
    image.width,
    image.height,
    profile.denseGridTargetWidth,
  );
  const originalPsms = [profile.textRegionPsm, "3", "11"];
  const total = crops.length + originalPsms.length;
  const original = await renderTileToCanvas(
    image,
    planImageTiles(image.width, image.height, profile)[0],
    profile.imagePreprocessing.includes("ocr_border")
      ? profile.ocrBorderPixels
      : 0,
  );
  if (!original) throw new Error("Could not create browser OCR canvas");
  const originalBlob = await canvasToJpegBlob(original);
  if (!originalBlob) throw new Error("Could not encode browser OCR image");
  for (const [index, pageSegmentationMode] of originalPsms.entries()) {
    yield {
      input: originalBlob,
      index,
      total,
      pageSegmentationMode,
    };
  }

  for (const [index, crop] of crops.entries()) {
    const canvas = await renderTileToCanvas(image, crop);
    if (!canvas) throw new Error("Could not create dense-grid OCR canvas");
    eraseDenseGridLines(canvas);
    const blob = await canvasToJpegBlob(canvas);
    if (!blob) throw new Error("Could not encode dense-grid OCR tile");
    yield {
      input: blob,
      index: index + originalPsms.length,
      total,
      pageSegmentationMode: crop.pageSegmentationMode,
    };
    await yieldToBrowser();
  }
}

async function* streamWideLandscapeCanvas(
  image: CanvasImageSource & ImageSize,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const tile = planImageTiles(image.width, image.height, profile)[0];
  const canvas = await renderTileToCanvas(
    image,
    tile,
    profile.imagePreprocessing.includes("ocr_border")
      ? profile.ocrBorderPixels
      : 0,
  );
  if (!canvas) throw new Error("Could not create wide-page OCR canvas");
  const blob = await canvasToJpegBlob(canvas);
  if (!blob) throw new Error("Could not encode wide-page OCR image");
  const psms = [profile.textRegionPsm, "3", "11", "12"];
  for (const [index, pageSegmentationMode] of psms.entries()) {
    yield {
      input: blob,
      index,
      total: psms.length,
      pageSegmentationMode,
    };
  }
}

async function* streamSparseCoverCanvas(
  image: CanvasImageSource & ImageSize,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const fullTile = planImageTiles(image.width, image.height, profile)[0];
  const passes = [
    { tile: fullTile, psm: profile.textRegionPsm, eraseLines: false },
    { tile: fullTile, psm: "11", eraseLines: false },
    { tile: fullTile, psm: "12", eraseLines: false },
    {
      tile: {
        sourceX: 0,
        sourceY: 0,
        sourceWidth: Math.max(1, Math.round(image.width * 0.55)),
        sourceHeight: Math.min(
          image.height,
          Math.max(1, Math.round(image.width * 0.15)),
        ),
        targetWidth: Math.max(1, Math.round(image.width * 0.55)) * 2,
        targetHeight:
          Math.min(image.height, Math.max(1, Math.round(image.width * 0.15))) *
          2,
      },
      psm: "3",
      eraseLines: true,
    },
    {
      tile: {
        sourceX: 0,
        sourceY: 0,
        sourceWidth: Math.max(1, Math.round(image.width * 0.55)),
        sourceHeight: Math.min(
          image.height,
          Math.max(1, Math.round(image.width * 0.15)),
        ),
        targetWidth: Math.max(1, Math.round(image.width * 0.55)) * 2,
        targetHeight:
          Math.min(image.height, Math.max(1, Math.round(image.width * 0.15))) *
          2,
      },
      psm: "6",
      eraseLines: true,
    },
  ];

  for (const [index, pass] of passes.entries()) {
    const canvas = await renderTileToCanvas(image, pass.tile);
    if (!canvas) throw new Error("Could not create sparse-cover OCR canvas");
    if (pass.eraseLines) eraseDenseGridLines(canvas);
    const blob = await canvasToJpegBlob(canvas);
    if (!blob) throw new Error("Could not encode sparse-cover OCR image");
    yield {
      input: blob,
      index,
      total: passes.length,
      pageSegmentationMode: pass.psm,
    };
    await yieldToBrowser();
  }
}

function boundedImageSize(
  width: number,
  height: number,
  profile: BrowserOcrProfile,
): ImageSize {
  const scale = Math.min(
    1,
    profile.maxDimension / Math.max(width, height),
    Math.sqrt(profile.maxImagePixels / Math.max(1, width * height)),
  );
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

export function shouldTryProjectedDocumentDewarp(
  width: number,
  height: number,
): boolean {
  if (width <= 0 || height <= 0) return false;
  return (
    Math.max(width, height) / Math.max(1, Math.min(width, height)) <=
    MAX_PROJECTED_DOCUMENT_ASPECT_RATIO
  );
}

export function shouldTryProjectorSlideDewarp(
  width: number,
  height: number,
): boolean {
  if (width <= 0 || height <= 0) return false;
  const aspect = height / Math.max(1, width);
  return (
    width >= 850 &&
    width <= 1200 &&
    height >= 1100 &&
    height <= 1500 &&
    aspect >= 1.2 &&
    aspect <= 1.6
  );
}

async function prepareProjectorSlideBrowserImage(
  file: File,
  profile: BrowserOcrProfile,
): Promise<BrowserCanvas | null> {
  const image = await loadBrowserImage(file);
  if (!image) return null;

  try {
    if (!shouldTryProjectorSlideDewarp(image.width, image.height)) {
      return null;
    }
    const bounded = boundedImageSize(image.width, image.height, profile);
    const source = await renderTileToCanvas(image, {
      sourceX: 0,
      sourceY: 0,
      sourceWidth: image.width,
      sourceHeight: image.height,
      targetWidth: bounded.width,
      targetHeight: bounded.height,
    });
    if (!source) return null;
    return dewarpProjectorSlideCanvas(
      source,
      profile.maxImagePixels,
      profile.maxDimension,
    );
  } finally {
    if ("close" in image && typeof image.close === "function") {
      image.close();
    }
  }
}

async function prepareDewarpedBrowserImage(
  file: File,
  profile: BrowserOcrProfile,
): Promise<BrowserCanvas | null> {
  const image = await loadBrowserImage(file);
  if (!image) return null;

  try {
    if (!shouldTryProjectedDocumentDewarp(image.width, image.height)) {
      return null;
    }
    const bounded = boundedImageSize(image.width, image.height, profile);
    const source = await renderTileToCanvas(image, {
      sourceX: 0,
      sourceY: 0,
      sourceWidth: image.width,
      sourceHeight: image.height,
      targetWidth: bounded.width,
      targetHeight: bounded.height,
    });
    if (!source) return null;
    return dewarpProjectedDocumentCanvas(
      source,
      profile.maxImagePixels,
      profile.maxDimension,
    );
  } finally {
    if ("close" in image && typeof image.close === "function") {
      image.close();
    }
  }
}

async function* streamCanvasTiles(
  image: CanvasImageSource & ImageSize,
  profile: BrowserOcrProfile,
  pageSegmentationMode?: string,
  borderOverride?: number,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const tiles = planImageTiles(image.width, image.height, profile);
  const borderPixels =
    borderOverride ??
    (profile.imagePreprocessing.includes("ocr_border")
      ? profile.ocrBorderPixels
      : 0);
  for (const [index, tile] of tiles.entries()) {
    const canvas = await renderTileToCanvas(image, tile, borderPixels);
    if (!canvas) throw new Error("Could not create browser OCR canvas");

    const blob = await canvasToJpegBlob(canvas);
    if (!blob) throw new Error("Could not encode browser OCR tile");
    yield { input: blob, index, total: tiles.length, pageSegmentationMode };
    await yieldToBrowser();
  }
}

async function* streamInBrowserCanvas(
  file: File,
  profile: BrowserOcrProfile,
): AsyncGenerator<PreparedBrowserOcrInput> {
  const image = await loadBrowserImage(file);
  if (!image) return;

  try {
    yield* streamCanvasTiles(image, profile);
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

  if (profile.denseGridFallback) {
    const image = await loadBrowserImage(file);
    if (image) {
      try {
        if (await looksLikeDenseGridImage(image)) {
          yield* streamDenseGridCanvas(image, profile);
          return;
        }
        if (await looksLikeSparseCoverImage(image)) {
          yield* streamSparseCoverCanvas(image, profile);
          return;
        }
        if (await looksLikeEdgeToEdgeWordImage(image)) {
          yield* streamCanvasTiles(image, profile, profile.edgeWordFallbackPsm);
          return;
        }
        if (
          image.width >= 2200 &&
          image.height >= 1500 &&
          image.height / Math.max(1, image.width) <= 0.9
        ) {
          yield* streamWideLandscapeCanvas(image, profile);
          return;
        }
      } finally {
        if ("close" in image && typeof image.close === "function") {
          image.close();
        }
      }
    }
  }

  if (profile.imagePreprocessing.includes("projector_slide_dewarp")) {
    const dewarped = await prepareProjectorSlideBrowserImage(file, profile);
    if (dewarped) {
      yield* streamCanvasTiles(
        dewarped,
        profile,
        undefined,
        profile.ocrBorderPixels,
      );
      yield* streamCanvasTiles(
        dewarped,
        profile,
        undefined,
        profile.ocrBorderPixels * 2,
      );
      return;
    }
  }

  if (profile.imagePreprocessing.includes("projected_document_dewarp")) {
    const dewarped = await prepareDewarpedBrowserImage(file, profile);
    if (dewarped) {
      yield* streamCanvasTiles(dewarped, profile, "3");
      return;
    }
  }

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

  let emitted = false;
  for await (const prepared of streamInBrowserCanvas(file, profile)) {
    emitted = true;
    yield prepared;
  }
  if (!emitted) yield { input: file, index: 0, total: 1 };
}
