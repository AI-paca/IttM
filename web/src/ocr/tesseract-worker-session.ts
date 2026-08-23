import { createWorker } from "tesseract.js";
import type { BrowserOcrProfile } from "./browser-profile";
import { scoreMathLanguage } from "./math-language";
import {
  loadBrowserPipelineCore,
  type BrowserPipelineCore,
} from "./pipeline-core";
import {
  fallbackEvidenceTokens,
  ocrCompactCharCount,
  sharedOcrTokenCount,
} from "./text-block-metrics";
import { toTesseractRecognizeInput } from "./tesseract-recognize-input";
import type { ProgressSink } from "./types";

interface TesseractLoggerMessage {
  status?: string;
  progress?: number;
}

interface TesseractWorkerLike {
  setParameters?(params: Record<string, string>): Promise<unknown>;
  reinitialize?(languages: string): Promise<unknown>;
  recognize(
    input: unknown,
    options?: Record<string, unknown>,
    output?: Record<string, boolean>,
  ): Promise<{ data: TesseractPageLike }>;
  terminate(): Promise<void>;
}

interface TesseractBboxLike {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface TesseractWordLike {
  text?: string;
  confidence?: number;
  bbox?: TesseractBboxLike;
}

interface TesseractLineLike {
  words?: TesseractWordLike[];
}

interface TesseractParagraphLike {
  lines?: TesseractLineLike[];
}

interface TesseractBlockLike {
  paragraphs?: TesseractParagraphLike[];
}

interface TesseractPageLike {
  text: string;
  confidence?: number;
  blocks?: TesseractBlockLike[] | null;
}

export interface BrowserOcrWordBox {
  text: string;
  confidence: number | null;
  bbox: {
    x0: number;
    y0: number;
    x1: number;
    y1: number;
  };
}

export interface BrowserOcrDetailedResult {
  text: string;
  words: BrowserOcrWordBox[];
  confidence?: number;
}

type SpanEvidenceScorer = Pick<BrowserPipelineCore, "spanEvidenceScore">;
type BrowserOcrDecisionCore = Pick<
  BrowserPipelineCore,
  "spanEvidenceScore" | "shouldReplacePrimary"
>;
type PipelineCoreLoader = () => Promise<BrowserOcrDecisionCore>;

type BrowserCanvasLike = OffscreenCanvas | HTMLCanvasElement;

function createCanvas(width: number, height: number): BrowserCanvasLike | null {
  if (typeof OffscreenCanvas !== "undefined")
    return new OffscreenCanvas(width, height);
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

function clampByte(value: number): number {
  if (value < 0) return 0;
  if (value > 255) return 255;
  return Math.round(value);
}

async function loadBrowserImage(
  input: Blob,
): Promise<
  (ImageBitmap & { width: number; height: number }) | HTMLImageElement | null
> {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(input);
    } catch {
      // fall back
    }
  }

  if (
    typeof document === "undefined" ||
    typeof Image === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    return null;
  }
  return await new Promise<HTMLImageElement | null>((resolve) => {
    const image = new Image();
    const objectUrl = URL.createObjectURL(input);

    image.onload = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(image);
    };

    image.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(null);
    };

    image.src = objectUrl;
  });
}

async function canvasToImageBlob(
  canvas: BrowserCanvasLike,
): Promise<Blob | null> {
  if ("convertToBlob" in canvas) {
    return await canvas.convertToBlob({ type: "image/png" });
  }
  return await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((blob) => resolve(blob), "image/png");
  });
}

async function buildImageVariants(
  input: Blob,
  maxPixels: number,
): Promise<Blob[]> {
  const variants: Blob[] = [];
  if (
    typeof OffscreenCanvas === "undefined" &&
    typeof document === "undefined"
  ) {
    return variants;
  }

  const image = await loadBrowserImage(input);
  if (!image) return variants;

  const width = image.width;
  const height = image.height;
  if (width <= 1 || height <= 1 || width * height > maxPixels) {
    if ("close" in image && typeof image.close === "function") image.close();
    return variants;
  }

  const sourceCanvas = createCanvas(width, height);
  if (!sourceCanvas) {
    if ("close" in image && typeof image.close === "function") image.close();
    return variants;
  }

  const sourceContext = sourceCanvas.getContext("2d");
  if (!sourceContext) {
    if ("close" in image && typeof image.close === "function") image.close();
    return variants;
  }

  sourceContext.fillStyle = "white";
  sourceContext.fillRect(0, 0, width, height);
  sourceContext.drawImage(image, 0, 0, width, height);
  if ("close" in image && typeof image.close === "function") image.close();

  const addCanvasVariant = async (
    mutation?: (imageData: Uint8ClampedArray) => void,
  ) => {
    const variantCanvas = createCanvas(width, height);
    if (!variantCanvas) return;
    const context = variantCanvas.getContext("2d");
    if (!context) return;
    context.drawImage(sourceCanvas, 0, 0);
    if (mutation) {
      const imageData = context.getImageData(0, 0, width, height);
      mutation(imageData.data);
      context.putImageData(imageData, 0, 0);
    }
    const blob = await canvasToImageBlob(variantCanvas);
    if (blob) variants.push(blob);
  };

  await addCanvasVariant((data) => {
    for (let index = 0; index < data.length; index += 4) {
      data[index] = 255 - data[index];
      data[index + 1] = 255 - data[index + 1];
      data[index + 2] = 255 - data[index + 2];
    }
  });
  await addCanvasVariant((data) => {
    const contrast = 1.45;
    const factor = (259 * (contrast + 255)) / (255 * (259 - contrast));
    for (let index = 0; index < data.length; index += 4) {
      data[index] = clampByte((data[index] - 128) * factor + 128);
      data[index + 1] = clampByte((data[index + 1] - 128) * factor + 128);
      data[index + 2] = clampByte((data[index + 2] - 128) * factor + 128);
    }
  });
  await addCanvasVariant((data) => {
    const histogram = new Uint32Array(256);
    const gray = new Uint8Array(data.length / 4);
    for (
      let index = 0, pixel = 0;
      index < data.length;
      index += 4, pixel += 1
    ) {
      const level = Math.round(
        data[index] * 0.299 + data[index + 1] * 0.587 + data[index + 2] * 0.114,
      );
      gray[pixel] = level;
      histogram[level] += 1;
    }
    const total = gray.length;
    let weightedTotal = 0;
    for (let level = 0; level < histogram.length; level += 1) {
      weightedTotal += level * histogram[level];
    }
    let backgroundWeight = 0;
    let backgroundTotal = 0;
    let threshold = 127;
    let bestVariance = -1;
    for (let level = 0; level < histogram.length; level += 1) {
      backgroundWeight += histogram[level];
      if (backgroundWeight === 0) continue;
      const foregroundWeight = total - backgroundWeight;
      if (foregroundWeight === 0) break;

      backgroundTotal += level * histogram[level];
      const backgroundMean = backgroundTotal / backgroundWeight;
      const foregroundMean =
        (weightedTotal - backgroundTotal) / foregroundWeight;
      const variance =
        backgroundWeight *
        foregroundWeight *
        (backgroundMean - foregroundMean) ** 2;
      if (variance > bestVariance) {
        bestVariance = variance;
        threshold = level;
      }
    }
    for (let index = 0; index < data.length; index += 4) {
      const light = Math.round(
        data[index] * 0.299 + data[index + 1] * 0.587 + data[index + 2] * 0.114,
      );
      const value = light <= threshold ? 0 : 255;
      data[index] = value;
      data[index + 1] = value;
      data[index + 2] = value;
    }
  });

  return variants;
}

function ocrGarbageRatio(text: string): number {
  const characters = Array.from(text).filter((character) => character.trim());
  if (!characters.length) return 1;
  const allowedSymbols = "._:/+-%()[]№₽$€|,;\"'";
  const weird = characters.filter(
    (character) =>
      !/[\p{L}\p{N}]/u.test(character) && !allowedSymbols.includes(character),
  ).length;
  const tokens = text.match(/[\p{L}\p{N}_]+(?:[.+:/-][\p{L}\p{N}_]+)*/gu) ?? [];
  const shortTokens = tokens.filter(
    (token) => Array.from(token).length <= 1,
  ).length;
  const shortTokenRatio =
    tokens.length >= 6 ? shortTokens / Math.max(1, tokens.length) : 0;
  return Math.max(weird / characters.length, shortTokenRatio);
}

function isStrongImageCandidate(result: BrowserOcrDetailedResult): boolean {
  const text = result.text.trim();
  if (!text) return false;
  const tokenCount = text.match(/[\p{L}\p{N}_]+/gu)?.length ?? 0;
  const confidence = result.confidence ?? 0;
  const garbageRatio = ocrGarbageRatio(text);
  if (garbageRatio >= 0.16) return false;
  return (
    (tokenCount >= 4 && confidence >= 70) ||
    (tokenCount >= 2 && confidence >= 88 && garbageRatio < 0.12)
  );
}

interface TesseractWorkerOptions {
  langPath?: string;
  cachePath?: string;
  gzip?: boolean;
  workerPath?: string;
  corePath?: string;
  workerBlobURL?: boolean;
  logger?: (message: TesseractLoggerMessage) => void;
}

const LANGUAGE_SCRIPTS: Record<string, string> = {
  eng: "latin",
  rus: "cyrillic",
  kaz: "cyrillic",
  kir: "cyrillic",
  chi_sim: "cjk",
  chi_tra: "cjk",
  ell: "greek",
  equ: "math",
};

const REVIEWER_EMPTY_STATE = "empty";
const REVIEWER_EXTRA_LANGUAGES = ["ell", "equ"];
const REVIEWER_STATE_TYPES = [REVIEWER_EMPTY_STATE];

const SCRIPT_PATTERNS: Record<string, RegExp> = {
  latin: /[A-Za-z]/g,
  cyrillic: /[\u0400-\u04ff]/g,
  cjk: /[\u3400-\u9fff]/g,
  greek: /[\u0370-\u03ff]/g,
  math: /[\u2200-\u22ff+\-*/=<>^_√∫ΣΠπ∞≈≠≤≥]/g,
};

type CreateWorkerFn = (
  languages: string,
  oem: number,
  options: TesseractWorkerOptions,
) => Promise<TesseractWorkerLike>;

async function createTesseractWorker(
  languages: string,
  oem: number,
  options: TesseractWorkerOptions,
): Promise<TesseractWorkerLike> {
  return (await createWorker(
    languages,
    oem,
    options as Parameters<typeof createWorker>[2],
  )) as unknown as TesseractWorkerLike;
}

const compiledTesseractAssetRoot = `${
  import.meta.env?.BASE_URL ?? "/"
}vendor/tesseract/`;
const compiledTesseractWorkerUrl = `${
  import.meta.env?.BASE_URL ?? "/"
}vendor/tesseract/worker.min.js`;

export function normalizeAppBaseUrl(base: string | undefined): string {
  if (!base || base === "./") return "/";
  return base.endsWith("/") ? base : `${base}/`;
}

function browserTesseractOptions(): Partial<TesseractWorkerOptions> {
  if (typeof window === "undefined") {
    return {};
  }

  return {
    workerPath: compiledTesseractWorkerUrl,
    corePath: compiledTesseractAssetRoot,
    workerBlobURL: false,
  };
}

function cacheKey(profile: BrowserOcrProfile): string {
  return JSON.stringify({
    languages: profile.languages,
    availableLanguages: (profile.availableLanguages || []).join("+"),
    langPath: profile.langPath || "",
    cachePath: profile.cachePath || "",
    gzip: profile.gzip ?? null,
    textRegionPsm: profile.textRegionPsm,
    edgeWordFallbackPsm: profile.edgeWordFallbackPsm,
    edgeWordFallbackMinTokens: profile.edgeWordFallbackMinTokens,
    ocrLanguageRetry: profile.ocrLanguageRetry,
    lexicalCorrection: profile.lexicalCorrection,
    tableSlotBuilder: profile.tableSlotBuilder,
    tableSlotMaxColumns: profile.tableSlotMaxColumns,
  });
}

function languageScript(language: string): string {
  return LANGUAGE_SCRIPTS[language] || "latin";
}

function splitLanguages(languages: string): string[] {
  return Array.from(new Set(languages.split("+").filter(Boolean)));
}

function reviewerRetryLanguages(
  languages: string,
  availableLanguages?: readonly string[],
): string[] {
  const configured = splitLanguages(languages);
  const available = new Set(availableLanguages || configured);
  return Array.from(
    new Set([
      ...configured,
      ...REVIEWER_EXTRA_LANGUAGES.filter((language) => available.has(language)),
    ]),
  );
}

function reviewerAgendaStates(
  languages: string,
  availableLanguages?: readonly string[],
): string[] {
  return Array.from(
    new Set([
      ...reviewerRetryLanguages(languages, availableLanguages),
      ...REVIEWER_STATE_TYPES,
    ]),
  );
}

function normalizeProbabilities(
  probabilities: Record<string, number>,
): Record<string, number> {
  const total = Object.values(probabilities).reduce(
    (sum, probability) => sum + Math.max(0, probability),
    0,
  );
  const languages = Object.keys(probabilities);
  if (total <= 0) {
    const fallback = languages.length ? 1 / languages.length : 1;
    return Object.fromEntries(
      languages.map((language) => [language, fallback]),
    );
  }
  return Object.fromEntries(
    languages.map((language) => [
      language,
      Math.max(0, probabilities[language] || 0) / total,
    ]),
  );
}

function initialLanguageProbabilities(
  languages: string,
  availableLanguages?: readonly string[],
): Record<string, number> {
  const configured = splitLanguages(languages);
  const singles = configured.length ? configured : ["eng"];
  const weights = Object.fromEntries(
    reviewerAgendaStates(singles.join("+"), availableLanguages).map(
      (language) => [language, singles.includes(language) ? 1 : 0.05],
    ),
  );
  return normalizeProbabilities(weights);
}

function countPattern(text: string, pattern: RegExp): number {
  return text.match(pattern)?.length ?? 0;
}

function numericTextEvidence(text: string): number {
  const numericTokens =
    text.match(
      /(?<![\p{L}\p{N}_])[+-]?\d+(?:[.,:/-]\d+)*%?(?![\p{L}\p{N}_])/gu,
    ) ?? [];
  if (!numericTokens.length) return 0;
  const digitCount = text.match(/\p{N}/gu)?.length ?? 0;
  const letterCount = text.match(/\p{L}/gu)?.length ?? 0;
  if (letterCount === 0 && numericTokens.length === 1) {
    return digitCount;
  }
  if (numericTokens.length < 2 && digitCount < Math.max(2, letterCount)) {
    return 0;
  }
  return digitCount;
}

function languageEvidence(
  text: string,
  priors: Record<string, number>,
): Record<string, number> {
  if (!text.trim() && priors[REVIEWER_EMPTY_STATE] !== undefined) {
    return { [REVIEWER_EMPTY_STATE]: 1 };
  }

  const scriptCounts = Object.fromEntries(
    Object.entries(SCRIPT_PATTERNS).map(([script, pattern]) => [
      script,
      countPattern(text, pattern),
    ]),
  );
  const mathLanguageScores = scoreMathLanguage(text);
  const evidence = Object.fromEntries(
    Object.keys(priors).map((language) => [
      language,
      (languageScript(language) === "math"
        ? (scriptCounts[languageScript(language)] || 0) * 3 +
          numericTextEvidence(text)
        : scriptCounts[languageScript(language)] || 0) +
        (mathLanguageScores[language as "equ" | "ell"] || 0),
    ]),
  );
  const total = Object.values(evidence).reduce((sum, value) => sum + value, 0);
  const alphaEvidence =
    (scriptCounts.latin || 0) +
    (scriptCounts.cyrillic || 0) +
    (scriptCounts.cjk || 0) +
    (scriptCounts.greek || 0);
  return total >= 2 || ((evidence.equ || 0) > 0 && alphaEvidence === 0)
    ? normalizeProbabilities(evidence)
    : {};
}

function updateLanguageProbabilities(
  priors: Record<string, number>,
  text: string,
): Record<string, number> {
  const evidence = languageEvidence(text, priors);
  if (!Object.keys(evidence).length) return priors;
  return normalizeProbabilities(
    Object.fromEntries(
      Object.keys(priors).map((language) => [
        language,
        priors[language] * 0.72 + (evidence[language] || 0) * 0.28,
      ]),
    ),
  );
}

function rankedLanguageCandidates(
  languages: string,
  priors: Record<string, number>,
  context: readonly string[] = [],
  includeLanguageRetries = true,
  availableLanguages?: readonly string[],
): string[] {
  const agendaStates = includeLanguageRetries
    ? reviewerAgendaStates(languages, availableLanguages)
    : REVIEWER_STATE_TYPES;
  const originalOrder = Object.fromEntries(
    agendaStates.map((language, index) => [language, index]),
  );
  const contextEvidence = languageEvidence(context.join(" "), priors);
  const score = (language: string): number => {
    return (priors[language] || 0) * 10 + (contextEvidence[language] || 0) * 8;
  };
  const ranked = [...agendaStates].sort(
    (left, right) =>
      score(right) - score(left) ||
      (originalOrder[left] || 0) - (originalOrder[right] || 0),
  );
  return Array.from(new Set([languages, ...ranked]));
}

interface ObservedOcrCandidate {
  result: BrowserOcrDetailedResult;
  languages: string;
}

function normalizedCandidateText(text: string): string {
  return text.trim().replace(/\s+/g, " ");
}

function scoreObservedCandidates(
  core: SpanEvidenceScorer,
  candidates: readonly ObservedOcrCandidate[],
  priors: Record<string, number>,
  countSourceAgreement = true,
): Array<ObservedOcrCandidate & { score: number }> {
  const agreement = new Map<string, number>();
  for (const candidate of candidates) {
    const text = normalizedCandidateText(candidate.result.text);
    if (text) agreement.set(text, (agreement.get(text) || 0) + 1);
  }
  return candidates.map((candidate) => {
    const text = candidate.result.text;
    const languages = splitLanguages(candidate.languages);
    const evidence = languageEvidence(text, priors);
    const scriptConsistency = languages.some((language) => {
      const script = languageScript(language);
      if (script === "math") return (evidence[language] || 0) > 0;
      const pattern = SCRIPT_PATTERNS[script];
      return pattern ? countPattern(text, pattern) > 0 : false;
    })
      ? 1
      : 0;
    let contextConsistency = languages.length
      ? languages.reduce((sum, language) => sum + (priors[language] || 0), 0) /
        languages.length
      : 0;
    if (
      languages.some((language) => language === "equ" || language === "ell") &&
      scoreMathLanguage(text)[languages.includes("equ") ? "equ" : "ell"] > 0
    ) {
      contextConsistency += 0.25;
    }
    const garbageRatio = ocrGarbageRatio(text);
    const contradictions =
      Number(!text.trim()) * 2 +
      Number(Boolean(text.trim()) && !/[\p{L}\p{N}]/u.test(text)) +
      Number(garbageRatio >= 0.16) +
      Number(
        Boolean(text.trim()) && languages.length > 0 && scriptConsistency === 0,
      ) *
        2;
    const score = core.spanEvidenceScore({
      ocrConfidenceMilli: Math.round(
        candidate.result.confidence === undefined
          ? 0
          : Math.max(0, Math.min(100, candidate.result.confidence)) * 10,
      ),
      scriptConsistencyMilli: Math.round(
        Math.max(0, Math.min(1, scriptConsistency)) * 1000,
      ),
      contextConsistencyMilli: Math.round(
        Math.max(0, Math.min(1, contextConsistency)) * 1000,
      ),
      sourceAgreement: Math.min(
        4,
        countSourceAgreement
          ? agreement.get(normalizedCandidateText(text)) || 0
          : Number(Boolean(text.trim())),
      ),
      contradictions,
    });
    return {
      ...candidate,
      score,
    };
  });
}

function normalizeWorkerError(error: unknown, workerPath?: string): Error {
  if (
    error instanceof Error &&
    error.message.startsWith("Не удалось запустить browser OCR worker")
  ) {
    return error;
  }

  let detail = "браузер не сообщил причину";
  if (error instanceof Error && error.message) {
    detail = error.message;
  } else if (typeof error === "string" && error.trim()) {
    detail = error.trim();
  } else if (error && typeof error === "object") {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) {
      detail = message.trim();
    }
  }

  const location = workerPath ? ` (${workerPath})` : "";
  return new Error(
    `Не удалось запустить browser OCR worker${location}: ${detail}.`,
  );
}

class BrowserOcrWorkerSession {
  private readonly key: string;
  private readonly profile: BrowserOcrProfile;
  private readonly workerPromise: Promise<TesseractWorkerLike>;
  private languageProbabilities: Record<string, number>;
  private progressSink: ProgressSink;
  private busy = false;

  constructor(
    profile: BrowserOcrProfile,
    onProgress: ProgressSink,
    createWorkerFn: CreateWorkerFn,
    private readonly corePromise: Promise<BrowserOcrDecisionCore>,
  ) {
    this.key = cacheKey(profile);
    this.profile = profile;
    this.languageProbabilities = initialLanguageProbabilities(
      profile.languages,
      profile.availableLanguages,
    );
    this.progressSink = onProgress;
    const workerOptions: TesseractWorkerOptions = {
      ...browserTesseractOptions(),
      ...(profile.langPath ? { langPath: profile.langPath } : {}),
      ...(profile.cachePath ? { cachePath: profile.cachePath } : {}),
      ...(profile.gzip !== undefined ? { gzip: profile.gzip } : {}),
      logger: (message) => this.reportProgress(message),
    };
    this.workerPromise = createWorkerFn(
      profile.languages,
      1,
      workerOptions,
    ).catch((error) => {
      throw normalizeWorkerError(error, workerOptions.workerPath);
    });
  }

  get isBusy(): boolean {
    return this.busy;
  }

  matches(profile: BrowserOcrProfile): boolean {
    return this.key === cacheKey(profile);
  }

  setProgressSink(onProgress: ProgressSink) {
    this.progressSink = onProgress;
  }

  async recognize(
    input: File | Blob,
    pageSegmentationMode?: string,
  ): Promise<string> {
    const { text } = await this.recognizePage(
      input,
      pageSegmentationMode,
      false,
    );
    return text;
  }

  async recognizeDetailed(
    input: File | Blob,
    pageSegmentationMode?: string,
  ): Promise<BrowserOcrDetailedResult> {
    return this.recognizePage(input, pageSegmentationMode, true);
  }

  async recognizeSeparatedBlock(
    input: File | Blob,
    pageSegmentationMode: string,
  ): Promise<string> {
    // Rust has already bounded the block. Re-running language and image
    // candidates here would reinitialize Tesseract for every segment.
    this.busy = true;
    try {
      const worker = await this.workerPromise;
      const recognizeInput = await toTesseractRecognizeInput(input);
      await worker.setParameters?.({
        tessedit_pageseg_mode: pageSegmentationMode,
      });
      const result = await this.recognizeWithOutput(
        worker,
        recognizeInput,
        false,
      );
      this.languageProbabilities = updateLanguageProbabilities(
        this.languageProbabilities,
        result.text,
      );
      return result.text;
    } finally {
      this.busy = false;
    }
  }

  private async recognizePage(
    input: File | Blob,
    pageSegmentationMode: string | undefined,
    detailed: boolean,
  ): Promise<BrowserOcrDetailedResult> {
    this.busy = true;
    try {
      const worker = await this.workerPromise;
      const recognizeInput = await toTesseractRecognizeInput(input);
      await worker.setParameters?.({
        tessedit_pageseg_mode:
          pageSegmentationMode || this.profile.textRegionPsm,
      });
      const primary = await this.recognizeWithImageCandidates(
        worker,
        input,
        recognizeInput,
        detailed,
      );
      if (primary.text.trim() || pageSegmentationMode) return primary;

      await worker.setParameters?.({
        tessedit_pageseg_mode: this.profile.edgeWordFallbackPsm,
      });
      const fallback = await this.recognizeWithImageCandidates(
        worker,
        input,
        recognizeInput,
        detailed,
      );
      const core = await this.corePromise;
      const primaryTokens = fallbackEvidenceTokens(primary.text);
      const fallbackTokens = fallbackEvidenceTokens(fallback.text);
      return core.shouldReplacePrimary({
        primaryChars: ocrCompactCharCount(primary.text),
        fallbackChars: ocrCompactCharCount(fallback.text),
        primaryTokens: primaryTokens.length,
        retainedPrimaryTokens: sharedOcrTokenCount(
          primaryTokens,
          fallbackTokens,
        ),
      })
        ? fallback
        : primary;
    } finally {
      this.busy = false;
    }
  }

  private async recognizeWithOutput(
    worker: TesseractWorkerLike,
    input: unknown,
    detailed: boolean,
  ): Promise<BrowserOcrDetailedResult> {
    const output = detailed ? { text: true, blocks: true } : undefined;
    const { data } = await worker.recognize(input, undefined, output);
    return {
      text: data.text,
      confidence:
        typeof data.confidence === "number" ? data.confidence : undefined,
      words: detailed ? extractWordBoxes(data) : [],
    };
  }

  private async prepareAdditionalRecognitionVariants(
    input: File | Blob,
  ): Promise<Array<File | Blob>> {
    if (
      this.profile.ocrLanguageRetry !== "t9_small" &&
      this.profile.lexicalCorrection !== "t9_small"
    ) {
      return [];
    }
    if (!(input instanceof Blob) || !input.type.startsWith("image/")) {
      return [];
    }
    return buildImageVariants(input, this.profile.maxImagePixels);
  }

  private async recognizeWithImageCandidates(
    worker: TesseractWorkerLike,
    sourceInput: File | Blob,
    recognizeInput: unknown,
    detailed: boolean,
  ): Promise<BrowserOcrDetailedResult> {
    const core = await this.corePromise;
    const primary = await this.recognizeWithLanguageCandidates(
      worker,
      recognizeInput,
      detailed,
    );
    const observed: ObservedOcrCandidate[] = [primary];
    if (!isStrongImageCandidate(primary.result)) {
      const variantInputs = await Promise.all(
        (await this.prepareAdditionalRecognitionVariants(sourceInput)).map(
          (variant) => toTesseractRecognizeInput(variant),
        ),
      );
      for (const input of variantInputs) {
        const selection = await this.recognizeWithLanguageCandidates(
          worker,
          input,
          detailed,
        );
        observed.push(selection);
      }
    }
    const best = scoreObservedCandidates(
      core,
      observed,
      this.languageProbabilities,
    ).reduce((selected, candidate) =>
      candidate.score > selected.score ? candidate : selected,
    );
    this.languageProbabilities = updateLanguageProbabilities(
      this.languageProbabilities,
      best.result.text,
    );
    return best.result;
  }

  private async recognizeWithLanguageCandidates(
    worker: TesseractWorkerLike,
    input: unknown,
    detailed: boolean,
  ): Promise<{ result: BrowserOcrDetailedResult; languages: string }> {
    const core = await this.corePromise;
    const candidates =
      this.profile.ocrLanguageRetry === "t9_small"
        ? rankedLanguageCandidates(
            this.profile.languages,
            this.languageProbabilities,
            [],
            Boolean(worker.reinitialize),
            this.profile.availableLanguages,
          )
        : [this.profile.languages];
    let activeLanguages = this.profile.languages;
    const observed: ObservedOcrCandidate[] = [];
    let hasEmptyState = false;

    try {
      for (const languages of candidates) {
        if (languages === REVIEWER_EMPTY_STATE) {
          hasEmptyState = true;
          continue;
        }
        if (languages !== activeLanguages) {
          if (!worker.reinitialize) continue;
          try {
            await worker.reinitialize?.(languages);
          } catch {
            continue;
          }
          activeLanguages = languages;
        }
        const result = await this.recognizeWithOutput(worker, input, detailed);
        observed.push({ result, languages });
      }
    } finally {
      if (activeLanguages !== this.profile.languages) {
        await worker.reinitialize?.(this.profile.languages);
      }
    }

    if (
      hasEmptyState &&
      observed.length > 0 &&
      observed.every(
        ({ result }) =>
          !/[\p{L}\p{N}]/u.test(result.text) ||
          ocrGarbageRatio(result.text) >= 0.16,
      )
    ) {
      return {
        result: { text: "", words: [] },
        languages: REVIEWER_EMPTY_STATE,
      };
    }
    const selected = scoreObservedCandidates(
      core,
      observed,
      this.languageProbabilities,
      false,
    ).reduce((best, candidate) =>
      candidate.score > best.score ? candidate : best,
    );
    return { result: selected.result, languages: selected.languages };
  }

  async terminate(): Promise<void> {
    try {
      await (await this.workerPromise).terminate();
    } catch {
      // The worker may already be gone after a cancelled run; cleanup should stay best-effort.
    }
  }

  private reportProgress(message: TesseractLoggerMessage) {
    const sink = this.progressSink;
    if (!sink) return;

    if (
      message.status === "recognizing text" &&
      message.progress !== undefined
    ) {
      sink(
        `Распознавание... ${Math.round(message.progress * 100)}%`,
        message.progress,
      );
    } else if (message.status) {
      sink(message.status);
    }
  }
}

function extractWordBoxes(data: TesseractPageLike): BrowserOcrWordBox[] {
  const words: BrowserOcrWordBox[] = [];
  for (const block of data.blocks || []) {
    for (const paragraph of block.paragraphs || []) {
      for (const line of paragraph.lines || []) {
        for (const word of line.words || []) {
          const text = (word.text || "").trim();
          const bbox = word.bbox;
          if (!text || !bbox) continue;
          words.push({
            text,
            confidence:
              typeof word.confidence === "number" ? word.confidence : null,
            bbox: {
              x0: bbox.x0,
              y0: bbox.y0,
              x1: bbox.x1,
              y1: bbox.y1,
            },
          });
        }
      }
    }
  }
  return words;
}

export class BrowserOcrWorkerLease {
  constructor(
    private readonly session: BrowserOcrWorkerSession,
    private readonly keepAlive: boolean,
  ) {}

  recognize(
    input: File | Blob,
    pageSegmentationMode?: string,
  ): Promise<string> {
    return this.session.recognize(input, pageSegmentationMode);
  }

  recognizeDetailed(
    input: File | Blob,
    pageSegmentationMode?: string,
  ): Promise<BrowserOcrDetailedResult> {
    return this.session.recognizeDetailed(input, pageSegmentationMode);
  }

  recognizeSeparatedBlock(
    input: File | Blob,
    pageSegmentationMode: string,
  ): Promise<string> {
    return this.session.recognizeSeparatedBlock(input, pageSegmentationMode);
  }

  async release(): Promise<void> {
    if (!this.keepAlive) {
      await this.session.terminate();
    }
  }
}

export class BrowserOcrWorkerPool {
  private cachedSession: BrowserOcrWorkerSession | null = null;

  constructor(
    private readonly createWorkerFn: CreateWorkerFn = createTesseractWorker,
    private readonly loadCore: PipelineCoreLoader = loadBrowserPipelineCore,
  ) {}

  async acquire(
    profile: BrowserOcrProfile,
    onProgress: ProgressSink,
  ): Promise<BrowserOcrWorkerLease> {
    if (!profile.cacheWorker) {
      return new BrowserOcrWorkerLease(
        new BrowserOcrWorkerSession(
          profile,
          onProgress,
          this.createWorkerFn,
          this.loadCore(),
        ),
        false,
      );
    }

    if (
      this.cachedSession &&
      this.cachedSession.matches(profile) &&
      !this.cachedSession.isBusy
    ) {
      this.cachedSession.setProgressSink(onProgress);
      return new BrowserOcrWorkerLease(this.cachedSession, true);
    }

    if (
      this.cachedSession &&
      !this.cachedSession.matches(profile) &&
      !this.cachedSession.isBusy
    ) {
      await this.cachedSession.terminate();
      this.cachedSession = null;
    }

    const session = new BrowserOcrWorkerSession(
      profile,
      onProgress,
      this.createWorkerFn,
      this.loadCore(),
    );
    if (!this.cachedSession) {
      this.cachedSession = session;
      return new BrowserOcrWorkerLease(session, true);
    }

    return new BrowserOcrWorkerLease(session, false);
  }

  async releaseCached(): Promise<void> {
    const session = this.cachedSession;
    this.cachedSession = null;
    await session?.terminate();
  }
}

const sharedWorkerPool = new BrowserOcrWorkerPool();

export function acquireBrowserOcrWorker(
  profile: BrowserOcrProfile,
  onProgress: ProgressSink,
): Promise<BrowserOcrWorkerLease> {
  return sharedWorkerPool.acquire(profile, onProgress);
}

export function releaseBrowserOcrWorkers(): Promise<void> {
  return sharedWorkerPool.releaseCached();
}
