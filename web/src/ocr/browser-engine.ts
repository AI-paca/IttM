import {
  createBrowserOcrProfile,
  type BrowserOcrProfile,
} from "./browser-profile";
import { streamImagesForBrowserOcr } from "./browser-image-preprocessor";
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

  const workerLease = await acquireBrowserOcrWorker(profile, onProgress);
  onProgress("Обработка изображения...");

  try {
    const chunks: string[] = [];
    let merged = "";
    for await (const prepared of streamImagesForBrowserOcr(file, profile)) {
      if (prepared.total > 1) {
        onProgress(
          `Обработка сегмента ${prepared.index + 1}/${prepared.total}...`,
        );
      }
      const text = await workerLease.recognize(prepared.input);
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
