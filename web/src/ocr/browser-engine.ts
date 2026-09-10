import {
  createBrowserOcrProfile,
  type BrowserOcrProfile,
} from "./browser-profile";
import { runBrowserSeparatedPipeline } from "./browser-separated";
import type { BrowserSeparatedDebugObserver } from "./browser-separated";
import {
  acquireBrowserOcrWorker,
  releaseBrowserOcrWorkers,
} from "./tesseract-worker-session";
import { applyBrowserLexicalCorrection } from "./lexical-correction";
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
  debugObserver?: BrowserSeparatedDebugObserver,
): Promise<OcrResult> {
  onProgress(`Загрузка OCR (${profile.languages}, ${profile.reason})...`);

  const workerLease = await acquireBrowserOcrWorker(profile, onProgress);
  onProgress("Обработка изображения...");

  try {
    const result = await runBrowserSeparatedPipeline(
      file,
      async (block, job) => {
        const pageSegmentationMode =
          job.recognitionMode === 2
            ? "11"
            : job.recognitionMode === 1
              ? "3"
              : profile.textRegionPsm;
        const recognized = await workerLease.recognizeSeparatedBlockDetailed(
          block,
          pageSegmentationMode,
          job.languages,
        );
        const text = applyBrowserLexicalCorrection(
          recognized.text,
          profile.lexicalCorrection,
        );
        return {
          text,
          confidenceMilli: 0,
          words: recognized.words.map((word) => ({
            text: word.text,
            bbox: [
              word.bbox.x0,
              word.bbox.y0,
              word.bbox.x1,
              word.bbox.y1,
            ] as const,
            confidenceMilli: Math.round((word.confidence ?? 0) * 10),
          })),
        };
      },
      onProgress,
      undefined,
      debugObserver,
    );
    if (result.markdown.trim()) {
      onChunkExtracted?.(result.markdown);
    }
    return {
      markdown: result.markdown,
      meta: {
        pipeline: "rust_separated_v1",
        pipeline_stages: result.stages,
        segments: result.jobs.length,
        pipeline_route_id: result.routeId,
      },
    };
  } finally {
    if (!profile.cacheWorker) onProgress("Зачистка памяти...");
    await workerLease.release();
  }
}
