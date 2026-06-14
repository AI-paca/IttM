import {
  createBrowserOcrProfile,
  type BrowserOcrProfile,
} from "./browser-profile";
import { prepareImagesForBrowserOcr } from "./browser-image-preprocessor";
import {
  acquireBrowserOcrWorker,
  releaseBrowserOcrWorkers,
} from "./tesseract-worker-session";
import { mergeOcrTextChunks } from "./merge-ocr-chunks";
import type { OcrResult, ProgressSink } from "./types";

export { createBrowserOcrProfile };
export type { BrowserOcrProfile };

export async function releaseBrowserOcrCache(): Promise<void> {
  await releaseBrowserOcrWorkers();
}

export async function runBrowserOcrLowMemory(
  file: File,
  onProgress: ProgressSink,
  onChunkExtracted?: (text: string) => void,
  profile: BrowserOcrProfile = createBrowserOcrProfile(null),
): Promise<OcrResult> {
  onProgress(`Загрузка OCR (${profile.languages}, ${profile.reason})...`);

  const inputs = await prepareImagesForBrowserOcr(file, profile);
  const workerLease = await acquireBrowserOcrWorker(profile, onProgress);
  onProgress("Обработка изображения...");

  try {
    const chunks: string[] = [];
    let merged = "";
    for (const [index, input] of inputs.entries()) {
      if (inputs.length > 1) {
        onProgress(`Обработка сегмента ${index + 1}/${inputs.length}...`);
      }
      const text = await workerLease.recognize(input);
      chunks.push(text);
      const nextMerged = mergeOcrTextChunks(chunks);
      onChunkExtracted?.(nextMerged.slice(merged.length));
      merged = nextMerged;
    }
    return { markdown: merged };
  } finally {
    if (!profile.cacheWorker) onProgress("Зачистка памяти...");
    await workerLease.release();
  }
}
