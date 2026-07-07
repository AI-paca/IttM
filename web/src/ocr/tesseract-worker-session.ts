import { createWorker } from "tesseract.js";
import type { BrowserOcrProfile } from "./browser-profile";
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

const REVIEWER_EXTRA_LANGUAGES = ["ell", "equ"];

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
    langPath: profile.langPath || "",
    cachePath: profile.cachePath || "",
    gzip: profile.gzip ?? null,
    textRegionPsm: profile.textRegionPsm,
    edgeWordFallbackPsm: profile.edgeWordFallbackPsm,
    edgeWordFallbackMinTokens: profile.edgeWordFallbackMinTokens,
    ocrLanguageRetry: profile.ocrLanguageRetry,
  });
}

function languageScript(language: string): string {
  return LANGUAGE_SCRIPTS[language] || "latin";
}

function splitLanguages(languages: string): string[] {
  return Array.from(new Set(languages.split("+").filter(Boolean)));
}

function reviewerRetryLanguages(languages: string): string[] {
  return Array.from(
    new Set([...splitLanguages(languages), ...REVIEWER_EXTRA_LANGUAGES]),
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
): Record<string, number> {
  const configured = splitLanguages(languages);
  const singles = configured.length ? configured : ["eng"];
  const weights = Object.fromEntries(
    reviewerRetryLanguages(singles.join("+")).map((language) => [
      language,
      singles.includes(language) ? 1 : 0.05,
    ]),
  );
  return normalizeProbabilities(weights);
}

function countPattern(text: string, pattern: RegExp): number {
  return text.match(pattern)?.length ?? 0;
}

function numericTextEvidence(text: string): number {
  const numericTokens =
    text.match(/(?<![\p{L}\p{N}_])[+-]?\d+(?:[.,:/-]\d+)*%?(?![\p{L}\p{N}_])/gu) ??
    [];
  if (!numericTokens.length) return 0;
  const digitCount = text.match(/\p{N}/gu)?.length ?? 0;
  const letterCount = text.match(/\p{L}/gu)?.length ?? 0;
  if (numericTokens.length < 2 && digitCount < Math.max(2, letterCount)) {
    return 0;
  }
  return digitCount;
}

function languageEvidence(
  text: string,
  priors: Record<string, number>,
): Record<string, number> {
  const scriptCounts = Object.fromEntries(
    Object.entries(SCRIPT_PATTERNS).map(([script, pattern]) => [
      script,
      countPattern(text, pattern),
    ]),
  );
  const evidence = Object.fromEntries(
    Object.keys(priors).map((language) => [
      language,
      languageScript(language) === "math"
        ? (scriptCounts[languageScript(language)] || 0) * 3 +
          numericTextEvidence(text)
        : scriptCounts[languageScript(language)] || 0,
    ]),
  );
  const total = Object.values(evidence).reduce((sum, value) => sum + value, 0);
  return total >= 2 ? normalizeProbabilities(evidence) : {};
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
  splayOrder: readonly string[],
  context: readonly string[] = [],
): string[] {
  const singles = reviewerRetryLanguages(languages);
  const originalOrder = Object.fromEntries(
    singles.map((language, index) => [language, index]),
  );
  const splayRank = Object.fromEntries(
    splayOrder.map((language, index) => [language, index]),
  );
  const contextEvidence = languageEvidence(context.join(" "), priors);
  const splaySize = Math.max(1, splayOrder.length);
  const score = (language: string): number => {
    const rank = Math.min(splayRank[language] ?? splaySize, splaySize);
    const splayScore = (splaySize - rank) / splaySize;
    return (
      (priors[language] || 0) * 10 +
      (contextEvidence[language] || 0) * 8 +
      splayScore * 4
    );
  };
  const ranked = [...singles].sort(
    (left, right) =>
      score(right) - score(left) ||
      (originalOrder[left] || 0) - (originalOrder[right] || 0),
  );
  return Array.from(new Set([languages, ...ranked]));
}

function promoteLanguages(
  order: readonly string[],
  languages: readonly string[],
): string[] {
  const promoted = [...order];
  for (const language of [...languages].reverse()) {
    const index = promoted.indexOf(language);
    if (index < 0) continue;
    promoted.splice(index, 1);
    promoted.unshift(language);
  }
  return promoted;
}

function selectedLanguageOrder(
  languages: string,
  text: string,
  priors: Record<string, number>,
): string[] {
  const selected = splitLanguages(languages);
  const evidence = languageEvidence(text, priors);
  return [...selected].sort(
    (left, right) => (evidence[right] || 0) - (evidence[left] || 0),
  );
}

function textQualityScore(text: string): number {
  const tokens = text.match(/[\p{L}\p{N}_]+(?:[.+:/-][\p{L}\p{N}_]+)*/gu);
  if (!tokens?.length) return 0;
  return tokens.reduce((score, token) => {
    const letters = token.match(/\p{L}/gu)?.length ?? 0;
    const digits = token.match(/\p{N}/gu)?.length ?? 0;
    const weird = Array.from(token).filter(
      (character) =>
        !/[\p{L}\p{N}]/u.test(character) &&
        !"._:/+-%()[]№₽$€".includes(character),
    ).length;
    const scriptCount = [
      /[A-Za-z]/,
      /[\u0400-\u04ff]/,
      /[\u3400-\u9fff]/,
      /[\u0370-\u03ff]/,
    ].filter((pattern) => pattern.test(token)).length;
    return (
      score +
      Math.max(1, token.length) +
      (letters || digits ? 2 : 0) +
      (token.length >= 3 ? 1 : 0) -
      (scriptCount > 1 && !digits ? 2 : 0) -
      weird * 2.5
    );
  }, 0);
}

function textCandidateScore(
  text: string,
  languages: string,
  priors: Record<string, number>,
): number {
  if (!text.trim()) return -1;
  const candidateLanguages = splitLanguages(languages);
  const priorScore = candidateLanguages.length
    ? candidateLanguages.reduce(
        (sum, language) => sum + (priors[language] || 0),
        0,
      ) / candidateLanguages.length
    : 0;
  const evidence = languageEvidence(text, priors);
  const evidenceScore = candidateLanguages.reduce(
    (sum, language) => sum + (evidence[language] || 0),
    0,
  );
  return textQualityScore(text) + priorScore * 1.5 + evidenceScore * 2;
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
  private languageSplayOrder: string[];
  private progressSink: ProgressSink;
  private busy = false;

  constructor(
    profile: BrowserOcrProfile,
    onProgress: ProgressSink,
    createWorkerFn: CreateWorkerFn,
  ) {
    this.key = cacheKey(profile);
    this.profile = profile;
    this.languageProbabilities = initialLanguageProbabilities(
      profile.languages,
    );
    this.languageSplayOrder = reviewerRetryLanguages(profile.languages);
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
      const primary = await this.recognizeWithLanguageCandidates(
        worker,
        recognizeInput,
        detailed,
      );
      if (primary.text.trim() || pageSegmentationMode) return primary;

      await worker.setParameters?.({
        tessedit_pageseg_mode: this.profile.edgeWordFallbackPsm,
      });
      const fallback = await this.recognizeWithLanguageCandidates(
        worker,
        recognizeInput,
        detailed,
      );
      const fallbackTokens =
        fallback.text.match(/[\p{L}\p{N}_]+/gu)?.length ?? 0;
      return fallbackTokens >= this.profile.edgeWordFallbackMinTokens
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
      words: detailed ? extractWordBoxes(data) : [],
    };
  }

  private async recognizeWithLanguageCandidates(
    worker: TesseractWorkerLike,
    input: unknown,
    detailed: boolean,
  ): Promise<BrowserOcrDetailedResult> {
    const candidates =
      this.profile.ocrLanguageRetry === "t9_small" && worker.reinitialize
        ? rankedLanguageCandidates(
            this.profile.languages,
            this.languageProbabilities,
            this.languageSplayOrder,
          )
        : [this.profile.languages];
    let activeLanguages = this.profile.languages;
    let best: BrowserOcrDetailedResult | null = null;
    let bestLanguages = "";
    let bestScore = -1;
    let primary: BrowserOcrDetailedResult | null = null;
    let primaryScore = -1;

    try {
      for (const languages of candidates) {
        if (languages !== activeLanguages) {
          try {
            await worker.reinitialize?.(languages);
          } catch {
            continue;
          }
          activeLanguages = languages;
        }
        const result = await this.recognizeWithOutput(worker, input, detailed);
        const score = textCandidateScore(
          result.text,
          languages,
          this.languageProbabilities,
        );
        if (languages === this.profile.languages) {
          primary = result;
          primaryScore = score;
        }
        if (score > bestScore) {
          best = result;
          bestLanguages = languages;
          bestScore = score;
        }
      }
    } finally {
      if (activeLanguages !== this.profile.languages) {
        await worker.reinitialize?.(this.profile.languages);
      }
    }

    const selected =
      bestLanguages !== this.profile.languages &&
      primary?.text.trim() &&
      bestScore < primaryScore + Math.max(12, primaryScore * 0.2)
        ? primary
        : best || { text: "", words: [] };
    const selectedLanguages =
      selected === primary ? this.profile.languages : bestLanguages;
    this.languageProbabilities = updateLanguageProbabilities(
      this.languageProbabilities,
      selected.text,
    );
    this.languageSplayOrder = promoteLanguages(
      this.languageSplayOrder,
      selectedLanguageOrder(
        selectedLanguages,
        selected.text,
        this.languageProbabilities,
      ),
    );
    return selected;
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
  ) {}

  async acquire(
    profile: BrowserOcrProfile,
    onProgress: ProgressSink,
  ): Promise<BrowserOcrWorkerLease> {
    if (!profile.cacheWorker) {
      return new BrowserOcrWorkerLease(
        new BrowserOcrWorkerSession(profile, onProgress, this.createWorkerFn),
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
