import {
  createBrowserOcrProfile,
  type BrowserOcrProfile,
} from "./browser-profile";
import { runBrowserSeparatedPipeline } from "./browser-separated";
import {
  acquireBrowserOcrWorker,
  releaseBrowserOcrWorkers,
} from "./tesseract-worker-session";
import type { OcrResult, ProgressSink } from "./types";
import { releaseBrowserTextReviewer } from "./text-reviewer";

export { createBrowserOcrProfile };
export type { BrowserOcrProfile };

export async function releaseBrowserOcrCache(): Promise<void> {
  await releaseBrowserOcrWorkers();
  releaseBrowserTextReviewer();
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
    const result = await runBrowserSeparatedPipeline(
      file,
      async (block) =>
        await workerLease.recognizeSeparatedBlock(block, profile.textRegionPsm),
      onProgress,
      (text) => {
        if (text.trim()) onChunkExtracted?.(`${text.trim()}\n`);
      },
    );
    return {
      markdown: result.markdown,
      meta: {
        pipeline: "rust_separated_v1",
        pipeline_stages: result.stages,
        segments: result.jobs.length,
      },
    };
  } finally {
    if (!profile.cacheWorker) onProgress("Зачистка памяти...");
    await workerLease.release();
  }
}
