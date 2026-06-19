import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "./browser-engine";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";

const fixtureRoot = resolve("ocr/tests/fixtures");
const expectedTokens = [
  "ABCXYZ",
  "abcxyz",
  "0123456789",
  "РУССКИЙ",
  "АБВГДЕЖЗ",
  "абвгдежз",
  "中文测试",
  "汉字识别",
  "MIXEDLATINД12345中文",
  "12345",
];

function resolveTessdataPath(): string {
  const candidates = [
    process.env.BROWSER_OCR_LANG_PATH,
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    resolve(".cache/tessdata"),
  ].filter((value): value is string => Boolean(value));

  const tessdataPath = candidates.find((candidate) =>
    ["eng", "rus", "chi_sim"].every((lang) =>
      existsSync(resolve(candidate, `${lang}.traineddata`)),
    ),
  );

  assert.ok(
    tessdataPath,
    "Browser OCR quality test requires local Tesseract traineddata for eng/rus/chi_sim. Install tesseract-ocr-eng, tesseract-ocr-rus, tesseract-ocr-chi-sim or set BROWSER_OCR_LANG_PATH.",
  );
  return tessdataPath;
}

function compact(text: string): string {
  return text.replace(/\s+/g, "");
}

async function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  onTimeout: () => Promise<void>,
  message: () => string,
): Promise<T> {
  let timeout: NodeJS.Timeout | undefined;
  const timeoutPromise = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      void onTimeout().finally(() => reject(new Error(message())));
    }, timeoutMs);
  });

  try {
    return await Promise.race([promise, timeoutPromise]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

test(
  "browser OCR recognizes strict English/Russian/Chinese fixture",
  { timeout: 180_000 },
  async (context) => {
    const tessdataPath = resolveTessdataPath();
    const fixture = resolve(fixtureRoot, "multilingual.png");
    if (!existsSync(fixture)) {
      try {
        execFileSync("python3", ["ocr/tests/support/quality_fixtures.py"], {
          stdio: "pipe",
        });
      } catch (error) {
        const stderr = String((error as { stderr?: Buffer }).stderr ?? "");
        const stdout = String((error as { stdout?: Buffer }).stdout ?? "");
        if (`${stdout}\n${stderr}`.includes("Noto CJK fonts")) {
          context.skip(
            "Noto CJK fonts are required to generate strict OCR fixtures.",
          );
          return;
        }
        throw error;
      }
    }
    const data = readFileSync(fixture);
    const file = new File([data], "multilingual.png", { type: "image/png" });
    const messages: string[] = [];

    try {
      const result = await withTimeout(
        runBrowserOcrLowMemory(
          file,
          (message) => messages.push(message),
          undefined,
          {
            languages: "rus+eng+chi_sim",
            cacheWorker: false,
            maxImagePixels: 20_000_000,
            maxDimension: 5000,
            pdfRenderScale: 1.5,
            reason: "ci-strict-multilingual",
            preprocessingProfile: "browser_tesseract_raw",
            imagePreprocessing: ["browser_resize", "ocr_border"],
            textRegionPsm: "6",
            denseGridFallback: true,
            denseGridTargetWidth: 3300,
            ocrBorderPixels: 10,
            edgeWordFallbackPsm: "7",
            edgeWordFallbackMinTokens: 1,
            layout: BROWSER_PIPELINE_PROFILES.browser_tesseract_raw.layout,
            langPath: tessdataPath,
            cachePath: resolve(".cache/tesseract-js"),
            gzip: false,
          },
        ),
        120_000,
        releaseBrowserOcrCache,
        () =>
          `Browser OCR timed out. Progress:\n${messages.join("\n") || "(no progress)"}`,
      );
      const recognized = compact(result.markdown);

      for (const token of expectedTokens) {
        assert.match(
          recognized,
          new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
          `Missing token ${token} in OCR output:\n${result.markdown}\nProgress:\n${messages.join("\n")}`,
        );
      }
    } finally {
      await releaseBrowserOcrCache();
    }
  },
);
