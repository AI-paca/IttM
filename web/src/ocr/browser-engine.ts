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
import { mergeOcrTextChunks, selectBetterOcrChunk } from "./merge-ocr-chunks";
import type { OcrResult, ProgressSink } from "./types";
import {
  releaseBrowserTextReviewer,
  reviewBrowserOcrCandidate,
} from "./text-reviewer";
import { loadBrowserPipelineCore, type PipelineStage } from "./pipeline-core";
import { runTextPipeline } from "./pipeline-orchestrator";

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

  const pipelineCore = await loadBrowserPipelineCore();
  const recipe = pipelineCore.recipe({});

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
      const text = !shouldUseBrowserTableSlots(prepared, profile)
        ? await workerLease.recognize(
            prepared.input,
            prepared.pageSegmentationMode,
          )
        : await recognizeBrowserTableAware(
            workerLease,
            prepared.input,
            profile,
            prepared.pageSegmentationMode,
            recipe,
          );
      if (prepared.total === 1 && prepared.index === 0 && chunks.length > 0) {
        chunks[chunks.length - 1] = selectBetterOcrChunk(
          chunks[chunks.length - 1],
          text,
        );
      } else {
        chunks.push(text);
      }
      const nextMerged = mergeOcrTextChunks(chunks, pipelineCore);
      if (nextMerged.length > merged.length) {
        onChunkExtracted?.(nextMerged.slice(merged.length));
      }
      merged = nextMerged;
    }
    return await runTextPipeline(
      { text: merged, capabilities: {} },
      {
        lexical_correction: (text) =>
          applyBrowserLexicalCorrection(text, profile.lexicalCorrection),
        group_structures: (text) =>
          alignedNumericTextToMarkdown(text)?.markdown ?? text,
        render_markdown: (text) =>
          applyContextualMarkdownGrammar(
            text,
            profile.contextualMarkdownGrammar,
          ),
      },
      pipelineCore,
    );
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
  recipe: ReadonlySet<PipelineStage> = new Set(),
): Promise<string> {
  const detailed = await workerLease.recognizeDetailed(
    input,
    pageSegmentationMode,
  );
  const options = {
    maxColumns: profile.tableSlotMaxColumns,
  };
  const tableMarkdown =
    recipe.has("select_language_candidate") &&
    profile.lexicalCorrection === "t9_small"
      ? await browserWordsToReviewedTableMarkdown(
          detailed,
          options,
          reviewBrowserOcrCandidate,
        )
      : browserWordsToTableMarkdown(detailed, options);
  return tableMarkdown
    ? selectBetterOcrChunk(detailed.text, tableMarkdown)
    : detailed.text;
}

export function shouldUseBrowserTableSlots(
  prepared: PreparedBrowserOcrInput,
  profile: BrowserOcrProfile,
): boolean {
  if (profile.tableSlotBuilder === "off") return false;
  if (!prepared.width || !prepared.height) return false;
  return prepared.width * prepared.height <= profile.maxImagePixels;
}
