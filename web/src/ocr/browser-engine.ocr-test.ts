import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import {
  createCanvas,
  DOMMatrix,
  DOMPoint,
  ImageData,
  loadImage,
} from "@napi-rs/canvas";
import {
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "./browser-engine";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";
import { createBrowserOcrProfile } from "./browser-profile";
import { configureBrowserPipelineCoreUrl } from "./pipeline-core";

const fixtureRoot = resolve("ocr/tests/fixtures");
const globalRecord = globalThis as unknown as Record<string, unknown>;
globalRecord.document = {
  createElement(tagName: string) {
    if (tagName !== "canvas") {
      throw new Error(`Unsupported test DOM element: ${tagName}`);
    }
    return createCanvas(1, 1);
  },
};
globalRecord.DOMMatrix = DOMMatrix;
globalRecord.DOMPoint = DOMPoint;
globalRecord.ImageData = ImageData;
globalRecord.createImageBitmap = async (blob: Blob) =>
  await loadImage(Buffer.from(await blob.arrayBuffer()));
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
  let sharedWorktreeCache: string | undefined;
  try {
    const commonGitDir = execFileSync(
      "git",
      ["rev-parse", "--git-common-dir"],
      { encoding: "utf8" },
    ).trim();
    sharedWorktreeCache = resolve(
      dirname(resolve(commonGitDir)),
      ".cache/tessdata",
    );
  } catch {
    sharedWorktreeCache = undefined;
  }
  const siblingCaches: string[] = [];
  try {
    const workspaceParents = new Set([
      dirname(process.cwd()),
      dirname(dirname(process.cwd())),
    ]);
    for (const parent of workspaceParents) {
      for (const entry of readdirSync(parent, { withFileTypes: true })) {
        if (entry.isDirectory()) {
          siblingCaches.push(resolve(parent, entry.name, ".cache/tessdata"));
        }
      }
    }
  } catch {
    // Explicit, shared-worktree and system paths below remain available.
  }
  const candidates = [
    process.env.BROWSER_OCR_LANG_PATH,
    sharedWorktreeCache,
    ...siblingCaches,
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
    const pipelineCore = readFileSync(
      resolve("web/public/wasm/ittm_pipeline_core.wasm"),
    );
    configureBrowserPipelineCoreUrl(
      `data:application/wasm;base64,${pipelineCore.toString("base64")}`,
    );
    const file = new File([data], "multilingual.png", { type: "image/png" });
    const messages: string[] = [];

    try {
      const result = await withTimeout(
        runBrowserOcrLowMemory(
          file,
          (message) => messages.push(message),
          undefined,
          {
            ...createBrowserOcrProfile(
              null,
              BROWSER_PIPELINE_PROFILES.browser_tesseract_dewarp,
            ),
            reason: "ci-production-multilingual",
            cacheWorker: false,
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
