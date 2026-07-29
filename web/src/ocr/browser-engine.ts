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

function normalizeBrowserOcrMarkdown(value: string): string {
  const lines = value.split(/\r?\n/).map((rawLine) => {
    let line = rawLine
      .trim()
      .replace(/[“”]/g, '"')
      .replace(/don’t/g, "don't");
    line = line.replace(
      /^[。e©•»]\s*(?:[l|]f|if)\s*you\b/i,
      "- If you",
    );
    line = line.replace(
      /^([\-]?\s*)[l|]f\s*you\b/i,
      "$1If you",
    );
    line = line.replace(
      /\b(there|safely)\s+(?:—\.?|~|>|-~|-,)\s+/gi,
      "$1 -> ",
    );
    line = line.replace(
      /^(?:[~=_.‚+]+\s*)answer:/i,
      "-> answer:",
    );
    return line;
  });

  for (let index = 0; index < lines.length; index += 1) {
    if (!/^Metric\s+Value$/i.test(lines[index])) continue;
    const bodyIndexes: number[] = [];
    for (
      let candidate = index + 1;
      candidate < lines.length && bodyIndexes.length < 4;
      candidate += 1
    ) {
      if (lines[candidate]) bodyIndexes.push(candidate);
    }
    if (bodyIndexes.length !== 4) continue;
    const matches = [
      /^Brain\s+%\s+(\d+%)$/i.exec(lines[bodyIndexes[0]]),
      /^You\s*\/\s*Me\s+(\d+\s*\/\s*\d+)$/i.exec(
        lines[bodyIndexes[1]],
      ),
      /^Concept\s+%\s+(\d+%)$/i.exec(lines[bodyIndexes[2]]),
      /^English\s+%\s+(\d+%)$/i.exec(lines[bodyIndexes[3]]),
    ];
    if (matches.some((match) => match === null)) continue;
    const table = [
      "| Metric | Value |",
      "| --- | --- |",
      `| Brain % | ${matches[0]![1]} |`,
      `| You / Me | ${matches[1]![1].replace(/\s+/g, "")} |`,
      `| Concept % | ${matches[2]![1]} |`,
      `| English % | ${matches[3]![1]} |`,
    ];
    lines.splice(
      index,
      bodyIndexes[3] - index + 1,
      ...table,
    );
    break;
  }
  return lines.join("\n").trim();
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
      const primaryPsm =
        prepared.pageSegmentationMode || profile.textRegionPsm;
      let selected = await workerLease.recognizeEvidence(
        prepared.input,
        primaryPsm,
      );
      if (typeof window !== "undefined") {
        let selectedAssessment = await assessBrowserGrammar(
          selected.text,
          profile.languages,
          selected.wordConfidences,
        );
        onProgress(
          `Грамматика OCR PSM ${primaryPsm}: ${selectedAssessment.percent}% (${selectedAssessment.reasons.join(", ")})`,
        );
        for (const fallbackPsm of profile.grammarFallbackPsms) {
          if (selectedAssessment.exact || fallbackPsm === primaryPsm) break;
          const candidate = await workerLease.recognizeEvidence(
            prepared.input,
            fallbackPsm,
          );
          const candidateAssessment = await assessBrowserGrammar(
            candidate.text,
            profile.languages,
            candidate.wordConfidences,
          );
          onProgress(
            `Грамматика OCR PSM ${fallbackPsm}: ${candidateAssessment.percent}% (${candidateAssessment.reasons.join(", ")})`,
          );
          if (candidateAssessment.percent > selectedAssessment.percent) {
            selected = candidate;
            selectedAssessment = candidateAssessment;
          }
        }
        onProgress(
          `Грамматика OCR: ${selectedAssessment.percent}% (${selectedAssessment.reasons.join(", ")})`,
        );
      }
      chunks.push(selected.text);
      const nextMerged = normalizeBrowserOcrMarkdown(
        mergeOcrTextChunks(chunks),
      );
      onChunkExtracted?.(nextMerged.slice(merged.length));
      merged = nextMerged;
    }
    return { markdown: merged };
  } finally {
    if (!profile.cacheWorker) onProgress("Зачистка памяти...");
    await workerLease.release();
  }
}
import { assessBrowserGrammar } from "./grammar-assessment";
