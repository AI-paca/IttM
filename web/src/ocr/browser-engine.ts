import {
  createBrowserOcrProfile,
  type BrowserOcrProfile,
} from "./browser-profile";
import { alignedNumericTextToMarkdown } from "./aligned-rows";
import {
  streamImagesForBrowserOcr,
  type PreparedBrowserOcrInput,
} from "./browser-image-preprocessor";
import {
  acquireBrowserOcrWorker,
  releaseBrowserOcrWorkers,
} from "./tesseract-worker-session";
import { applyContextualMarkdownGrammar } from "./contextual-markdown";
import {
  browserWordsToReviewedTableMarkdown,
  browserWordsToTableMarkdown,
} from "./browser-table-slots";
import { applyBrowserLexicalCorrection } from "./lexical-correction";
import { mergeOcrTextChunks } from "./merge-ocr-chunks";
import type { OcrResult, ProgressSink } from "./types";
import {
  releaseBrowserTextReviewer,
  reviewBrowserOcrCandidate,
} from "./text-reviewer";

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
    const chunks: string[] = [];
    let merged = "";
    for await (const prepared of streamImagesForBrowserOcr(file, profile)) {
      if (prepared.total > 1) {
        onProgress(
          `Обработка сегмента ${prepared.index + 1}/${prepared.total}...`,
        );
      }
      const text =
        !shouldUseBrowserTableSlots(prepared, profile)
          ? await workerLease.recognize(
              prepared.input,
              prepared.pageSegmentationMode,
            )
          : await recognizeBrowserTableAware(
              workerLease,
              prepared.input,
              profile,
              prepared.pageSegmentationMode,
            );
      chunks.push(text);
      const nextMerged = mergeOcrTextChunks(chunks);
      onChunkExtracted?.(nextMerged.slice(merged.length));
      merged = nextMerged;
    }
    const corrected = applyBrowserLexicalCorrection(
      merged,
      profile.lexicalCorrection,
    );
    const alignedRows = alignedNumericTextToMarkdown(corrected);
    return {
      markdown: applyContextualMarkdownGrammar(
        alignedRows?.markdown ?? corrected,
        profile.contextualMarkdownGrammar,
      ),
    };
  } finally {
    if (!profile.cacheWorker) onProgress("Зачистка памяти...");
    await workerLease.release();
  }
}

async function recognizeBrowserTableAware(
  workerLease: Awaited<ReturnType<typeof acquireBrowserOcrWorker>>,
  input: File | Blob,
  profile: BrowserOcrProfile,
  pageSegmentationMode?: string,
): Promise<string> {
  const detailed = await workerLease.recognizeDetailed(
    input,
    pageSegmentationMode,
  );
  const options = {
    maxColumns: profile.tableSlotMaxColumns,
  };
  const tableMarkdown =
    profile.lexicalCorrection === "t9_small"
      ? await browserWordsToReviewedTableMarkdown(
          detailed,
          options,
          reviewBrowserOcrCandidate,
        )
      : browserWordsToTableMarkdown(detailed, options);
  return tableMarkdown || detailed.text;
}

function shouldUseBrowserTableSlots(
  prepared: PreparedBrowserOcrInput,
  profile: BrowserOcrProfile,
): boolean {
  if (profile.tableSlotBuilder === "off") return false;
  if (prepared.total !== 1) return false;
  if (!prepared.width || !prepared.height) return false;
  return prepared.width * prepared.height <= profile.maxImagePixels;
}
