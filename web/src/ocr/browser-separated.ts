import {
  loadBrowserPipelineCore,
  SEPARATED_PIPELINE_STAGES,
  type SeparatedBlock,
  type SeparatedObject,
  type SeparatedOcrJob,
  type SeparatedOcrRaster,
  type SeparatedOcrWord,
} from "./pipeline-core";
import type { ProgressSink } from "./types";

export interface BrowserSeparatedRecognition {
  text: string;
  confidenceMilli?: number;
  words?: readonly SeparatedOcrWord[];
}

export interface BrowserSeparatedResult {
  markdown: string;
  jobs: readonly SeparatedOcrJob[];
  stages: readonly (typeof SEPARATED_PIPELINE_STAGES)[number][];
  routeId: number;
}

export interface BrowserSeparatedPlan {
  jobs: readonly SeparatedOcrJob[];
  objects: readonly SeparatedObject[];
  blocks: readonly SeparatedBlock[];
}

export interface BrowserSeparatedDebugObserver {
  input?(input: Blob): void | Promise<void>;
  planned?(plan: BrowserSeparatedPlan, routeId: number): void | Promise<void>;
  separateBlock?(image: Blob, block: SeparatedBlock): void | Promise<void>;
  block?(image: Blob, job: SeparatedOcrJob): void | Promise<void>;
  recognized?(
    text: string,
    confidenceMilli: number,
    job: SeparatedOcrJob,
    words: readonly SeparatedOcrWord[],
  ): void | Promise<void>;
}

type BrowserSeparatedRecognizer = (
  image: Blob,
  job: SeparatedOcrJob,
) => Promise<string | BrowserSeparatedRecognition>;

type RasterCanvas = OffscreenCanvas | HTMLCanvasElement;

function createRasterCanvas(width: number, height: number): RasterCanvas {
  if (typeof OffscreenCanvas !== "undefined") {
    return new OffscreenCanvas(width, height);
  }
  if (typeof document === "undefined") {
    throw new Error("Canvas is unavailable for the separated browser pipeline");
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

function context2d(canvas: RasterCanvas) {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("Could not create separated pipeline canvas");
  return context;
}

async function decodeRaster(input: Blob) {
  if (typeof createImageBitmap !== "function") {
    throw new Error("createImageBitmap is unavailable for browser OCR");
  }
  const bitmap = await createImageBitmap(input);
  const canvas = createRasterCanvas(bitmap.width, bitmap.height);
  const context = context2d(canvas);
  context.drawImage(bitmap, 0, 0);
  // Real browsers return ImageBitmap, while the local Node canvas adapter
  // intentionally exposes only the drawable width/height surface.
  bitmap.close?.();
  return { canvas, context, width: canvas.width, height: canvas.height };
}

async function canvasBlob(canvas: RasterCanvas): Promise<Blob> {
  if (
    typeof OffscreenCanvas !== "undefined" &&
    canvas instanceof OffscreenCanvas
  ) {
    return await canvas.convertToBlob({ type: "image/png" });
  }
  const htmlCanvas = canvas as HTMLCanvasElement;
  return await new Promise<Blob>((resolve, reject) => {
    htmlCanvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("Could not encode separated OCR block"));
    }, "image/png");
  });
}

async function rasterBlob(raster: SeparatedOcrRaster): Promise<Blob> {
  const canvas = createRasterCanvas(raster.width, raster.height);
  const context = context2d(canvas);
  const image = context.createImageData(raster.width, raster.height);
  for (
    let source = 0, target = 0;
    source < raster.pixels.length;
    source += 3, target += 4
  ) {
    image.data[target] = raster.pixels[source];
    image.data[target + 1] = raster.pixels[source + 1];
    image.data[target + 2] = raster.pixels[source + 2];
    image.data[target + 3] = 255;
  }
  context.putImageData(image, 0, 0);
  return await canvasBlob(canvas);
}

export async function runBrowserSeparatedPipeline(
  input: Blob,
  recognize: BrowserSeparatedRecognizer,
  onProgress?: ProgressSink,
  onSegment?: (text: string, job: SeparatedOcrJob) => void,
  debugObserver?: BrowserSeparatedDebugObserver,
): Promise<BrowserSeparatedResult> {
  const core = await loadBrowserPipelineCore();
  const routeId = core.routeId();
  await debugObserver?.input?.(input);
  onProgress?.(
    "preprocess → geometry → topology → find-object → separate-block...",
  );
  const raster = await decodeRaster(input);
  const imageData = raster.context.getImageData(
    0,
    0,
    raster.width,
    raster.height,
  );
  const session = core.beginSeparated({
    pixels: imageData.data,
    width: raster.width,
    height: raster.height,
    format: 4,
  });
  try {
    const jobs: SeparatedOcrJob[] = [];
    const plan = {
      jobs: session.jobs(),
      objects: session.objects(),
      blocks: session.blocks(),
    };
    await debugObserver?.planned?.(plan, routeId);
    for (const block of plan.blocks) {
      await debugObserver?.separateBlock?.(
        await rasterBlob(session.blockRaster(block.index)),
        block,
      );
    }
    session.startOcr();
    let index = 0;
    while (index < session.jobCount()) {
      const job = session.job(index);
      jobs.push(job);
      onProgress?.(`ocr-blocks ${job.index + 1}/${session.jobCount()}...`);
      const block = await rasterBlob(session.raster(job.index));
      await debugObserver?.block?.(block, job);
      const result = await recognize(block, job);
      const text = typeof result === "string" ? result : result.text;
      const confidenceMilli =
        typeof result === "string" ? 0 : (result.confidenceMilli ?? 0);
      const words = typeof result === "string" ? [] : (result.words ?? []);
      if (typeof result !== "string") {
        for (const word of words) {
          session.addOcrWord(job.index, word);
        }
      }
      session.setOcr(job.index, text, confidenceMilli);
      await debugObserver?.recognized?.(text, confidenceMilli, job, words);
      onSegment?.(text, job);
      index += 1;
    }
    onProgress?.("get-segment → generate-object...");
    session.runGetSegment();
    const markdown = session.render();
    const stages = session.completedStages();
    if (
      stages.length !== SEPARATED_PIPELINE_STAGES.length ||
      stages.some((stage, index) => stage !== SEPARATED_PIPELINE_STAGES[index])
    ) {
      throw new Error(
        `Separated browser pipeline stopped after: ${stages.join(", ")}`,
      );
    }
    return { markdown, jobs, stages, routeId };
  } finally {
    session.close();
  }
}
