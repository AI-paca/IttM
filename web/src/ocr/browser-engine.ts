import { createWorker } from "tesseract.js";
import type { AppDiagnostics, OcrResult, ProgressSink } from "./types";

const STRICT_LANGUAGES = "chi_sim+eng+rus";

type TesseractWorker = Awaited<ReturnType<typeof createWorker>>;

export interface BrowserOcrProfile {
  languages: string;
  cacheWorker: boolean;
  maxImagePixels: number;
  maxDimension: number;
  pdfRenderScale: number;
  reason: string;
  langPath?: string;
  cachePath?: string;
  gzip?: boolean;
}

let cachedWorker: Promise<TesseractWorker> | null = null;
let cachedLanguages = "";
let progressSink: ProgressSink | null = null;

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function createBrowserOcrProfile(
  diagnostics: AppDiagnostics | null,
): BrowserOcrProfile {
  const memory = numberOrNull(diagnostics?.browser.memory);
  const cores = numberOrNull(diagnostics?.browser.cores);
  const backendOffline = !diagnostics?.backend || Boolean(diagnostics?.error);

  if ((memory !== null && memory <= 2) || (cores !== null && cores <= 2)) {
    return {
      languages: STRICT_LANGUAGES,
      cacheWorker: false,
      maxImagePixels: 4_000_000,
      maxDimension: 2200,
      pdfRenderScale: 1,
      reason: "low-memory-browser",
    };
  }

  if (backendOffline || (memory !== null && memory <= 4)) {
    return {
      languages: STRICT_LANGUAGES,
      cacheWorker: true,
      maxImagePixels: 8_000_000,
      maxDimension: 3200,
      pdfRenderScale: 1.25,
      reason: "balanced-browser-fallback",
    };
  }

  return {
    languages: STRICT_LANGUAGES,
    cacheWorker: true,
    maxImagePixels: 14_000_000,
    maxDimension: 4200,
    pdfRenderScale: 1.5,
    reason: "quality-first",
  };
}

async function getBrowserWorker(
  profile: BrowserOcrProfile,
  onProgress: ProgressSink,
) {
  progressSink = onProgress;
  if (cachedWorker && cachedLanguages === profile.languages)
    return cachedWorker;

  await releaseBrowserOcrCache();
  cachedLanguages = profile.languages;
  cachedWorker = createWorker(profile.languages, 1, {
    ...(profile.langPath ? { langPath: profile.langPath } : {}),
    ...(profile.cachePath ? { cachePath: profile.cachePath } : {}),
    ...(profile.gzip !== undefined ? { gzip: profile.gzip } : {}),
    logger: (m) => {
      if (!progressSink) return;
      if (m.status === "recognizing text" && m.progress !== undefined) {
        progressSink(
          `Распознавание... ${Math.round(m.progress * 100)}%`,
          m.progress,
        );
      } else {
        progressSink(m.status);
      }
    },
  });
  return cachedWorker;
}

export async function releaseBrowserOcrCache(): Promise<void> {
  const worker = cachedWorker;
  cachedWorker = null;
  cachedLanguages = "";
  progressSink = null;

  if (!worker) return;
  try {
    await (await worker).terminate();
  } catch {
    // The worker may already be gone after a cancelled run; cleanup should stay best-effort.
  }
}

async function resizeImageIfNeeded(
  file: File,
  profile: BrowserOcrProfile,
): Promise<File | Blob> {
  if (!file.type.startsWith("image/") || typeof document === "undefined")
    return file;

  return new Promise<File | Blob>((resolve) => {
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);

    img.onload = () => {
      try {
        const pixels = img.width * img.height;
        const dimensionScale = Math.min(
          1,
          profile.maxDimension / Math.max(img.width, img.height),
        );
        const pixelScale = Math.min(
          1,
          Math.sqrt(profile.maxImagePixels / Math.max(pixels, 1)),
        );
        const scale = Math.min(dimensionScale, pixelScale);

        if (scale >= 0.999) {
          resolve(file);
          return;
        }

        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (!ctx) {
          resolve(file);
          return;
        }

        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(
          (blob) => {
            resolve(blob ?? file);
          },
          "image/jpeg",
          0.92,
        );
      } catch {
        resolve(file);
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    };

    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(file);
    };

    img.src = objectUrl;
  });
}

async function toRecognizeInput(input: File | Blob): Promise<unknown> {
  if (typeof document !== "undefined") return input;
  const bufferFactory = (
    globalThis as unknown as { Buffer?: { from(data: ArrayBuffer): unknown } }
  ).Buffer;
  if (bufferFactory && typeof input.arrayBuffer === "function") {
    return bufferFactory.from(await input.arrayBuffer());
  }
  return input;
}

export async function runBrowserOcrLowMemory(
  file: File,
  onProgress: ProgressSink,
  onChunkExtracted?: (text: string) => void,
  profile: BrowserOcrProfile = createBrowserOcrProfile(null),
): Promise<OcrResult> {
  onProgress(`Загрузка OCR (${profile.languages}, ${profile.reason})...`);

  const worker = await getBrowserWorker(profile, onProgress);
  const input = await resizeImageIfNeeded(file, profile);

  onProgress("Обработка изображения...");
  try {
    const recognizeInput = await toRecognizeInput(input);
    const {
      data: { text },
    } = await worker.recognize(recognizeInput as never);

    onChunkExtracted?.(text);
    return { markdown: text };
  } finally {
    if (!profile.cacheWorker) {
      onProgress("Зачистка памяти...");
      await releaseBrowserOcrCache();
    }
  }
}
