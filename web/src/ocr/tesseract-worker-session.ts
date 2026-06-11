import { createWorker } from "tesseract.js";
import type { BrowserOcrProfile } from "./browser-profile";
import { toTesseractRecognizeInput } from "./tesseract-recognize-input";
import type { ProgressSink } from "./types";

interface TesseractLoggerMessage {
  status?: string;
  progress?: number;
}

interface TesseractWorkerLike {
  recognize(input: unknown): Promise<{ data: { text: string } }>;
  terminate(): Promise<void>;
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

export function normalizeAppBaseUrl(base: string | undefined): string {
  if (!base || base === "./") return "/";
  return base.endsWith("/") ? base : `${base}/`;
}

function appBaseUrl(): string {
  const base = typeof document === "undefined" ? "/" : import.meta.env.BASE_URL;
  return normalizeAppBaseUrl(base);
}

function localTesseractAssetUrl(fileName = ""): string {
  return `${appBaseUrl()}vendor/tesseract/${fileName}`;
}

function browserTesseractOptions(): Partial<TesseractWorkerOptions> {
  if (typeof window === "undefined") {
    return {};
  }

  return {
    workerPath: localTesseractAssetUrl("worker.min.js"),
    corePath: localTesseractAssetUrl(),
    workerBlobURL: false,
  };
}

function cacheKey(profile: BrowserOcrProfile): string {
  return JSON.stringify({
    languages: profile.languages,
    langPath: profile.langPath || "",
    cachePath: profile.cachePath || "",
    gzip: profile.gzip ?? null,
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
  private readonly workerPromise: Promise<TesseractWorkerLike>;
  private progressSink: ProgressSink;
  private busy = false;

  constructor(
    profile: BrowserOcrProfile,
    onProgress: ProgressSink,
    createWorkerFn: CreateWorkerFn,
  ) {
    this.key = cacheKey(profile);
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

  async recognize(input: File | Blob): Promise<string> {
    this.busy = true;
    try {
      const worker = await this.workerPromise;
      const recognizeInput = await toTesseractRecognizeInput(input);
      const {
        data: { text },
      } = await worker.recognize(recognizeInput);
      return text;
    } finally {
      this.busy = false;
    }
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

export class BrowserOcrWorkerLease {
  constructor(
    private readonly session: BrowserOcrWorkerSession,
    private readonly keepAlive: boolean,
  ) {}

  recognize(input: File | Blob): Promise<string> {
    return this.session.recognize(input);
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
