import test from "node:test";
import assert from "node:assert/strict";
import type { BrowserOcrProfile } from "./browser-profile";
import {
  BrowserOcrWorkerPool,
  normalizeAppBaseUrl,
} from "./tesseract-worker-session";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";

function profile(): BrowserOcrProfile {
  return {
    languages: "eng",
    cacheWorker: true,
    maxImagePixels: 1_000_000,
    maxDimension: 1000,
    pdfRenderScale: 1,
    reason: "unit-test",
    preprocessingProfile: "browser_tesseract_raw",
    imagePreprocessing: ["browser_resize", "ocr_border"],
    textRegionPsm: "6",
    denseGridFallback: true,
    spatialFullPageFallback: false,
    darkUiTextFallback: false,
    contextualMarkdownGrammar: false,
    denseGridTargetWidth: 3300,
    ocrBorderPixels: 10,
    edgeWordFallbackPsm: "7",
    edgeWordFallbackMinTokens: 1,
    lexicalCorrection: "off",
    ocrLanguageRetry: "off",
    tableSlotBuilder: "off",
    tableSlotMaxColumns: 4,
    recursiveTableCellOcr: "auto",
    recursiveTableCellOcrBatchPixels: 8_000_000,
    layout: BROWSER_PIPELINE_PROFILES.browser_tesseract_raw.layout,
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("normalizes GitHub Pages base paths for local OCR assets", () => {
  assert.equal(normalizeAppBaseUrl("/IttM/"), "/IttM/");
  assert.equal(normalizeAppBaseUrl("/IttM"), "/IttM/");
  assert.equal(normalizeAppBaseUrl(undefined), "/");
});

test("worker pool uses local Tesseract worker and core assets", async () => {
  const globalRecord = globalThis as unknown as Record<string, unknown>;
  const previousWindow = globalRecord.window;
  globalRecord.window = {};

  let capturedOptions:
    | {
        workerPath?: string;
        corePath?: string;
        workerBlobURL?: boolean;
        logger?: (message: { status?: string; progress?: number }) => void;
      }
    | undefined;

  const workerFactory = async (
    _languages: string,
    _oem: number,
    options: {
      workerPath?: string;
      corePath?: string;
      workerBlobURL?: boolean;
      logger?: (message: { status?: string; progress?: number }) => void;
    },
  ) => {
    capturedOptions = options;
    return {
      async recognize() {
        return { data: { text: "" } };
      },
      async terminate() {},
    };
  };

  const pool = new BrowserOcrWorkerPool(workerFactory);
  try {
    const lease = await pool.acquire(profile(), () => {});

    assert.equal(
      capturedOptions?.workerPath,
      "/vendor/tesseract/worker.min.js",
    );
    assert.equal(capturedOptions?.corePath, "/vendor/tesseract/");
    assert.equal(capturedOptions?.workerBlobURL, false);

    await lease.release();
    await pool.releaseCached();
  } finally {
    if (previousWindow === undefined) {
      delete globalRecord.window;
    } else {
      globalRecord.window = previousWindow;
    }
  }
});

test("worker pool explains worker startup failures without a browser message", async () => {
  const globalRecord = globalThis as unknown as Record<string, unknown>;
  const previousWindow = globalRecord.window;
  globalRecord.window = {};

  const pool = new BrowserOcrWorkerPool(async () => {
    throw undefined;
  });

  try {
    const lease = await pool.acquire(profile(), () => {});
    await assert.rejects(
      () => lease.recognize(new Blob(["image"])),
      /Не удалось запустить browser OCR worker.*браузер не сообщил причину/,
    );
  } finally {
    await pool.releaseCached();
    if (previousWindow === undefined) {
      delete globalRecord.window;
    } else {
      globalRecord.window = previousWindow;
    }
  }
});

test("empty primary OCR retries with the profile edge-word PSM", async () => {
  const parameters: Record<string, string>[] = [];
  let recognition = 0;
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters(params) {
      parameters.push(params);
    },
    async recognize() {
      recognition += 1;
      return {
        data: {
          text: recognition === 1 ? "" : "SAMPLE",
        },
      };
    },
    async terminate() {},
  }));

  const lease = await pool.acquire(profile(), () => {});
  assert.equal(await lease.recognize(new Blob(["image"])), "SAMPLE");
  assert.deepEqual(parameters, [
    { tessedit_pageseg_mode: "6" },
    { tessedit_pageseg_mode: "7" },
  ]);
  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry ranks browser candidates from prior text", async () => {
  let activeLanguages = "rus+eng+chi_sim";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      const text =
        activeLanguages === "rus+eng+chi_sim"
          ? ""
          : activeLanguages === "eng"
            ? "PDF API"
            : activeLanguages === "chi_sim"
              ? ""
              : "РЕС";
      return { data: { text } };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng+chi_sim",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});

  assert.equal(await lease.recognize(new Blob(["first"])), "PDF API");
  assert.equal(await lease.recognize(new Blob(["second"])), "PDF API");
  assert.deepEqual(recognizedLanguages.slice(0, 4), [
    "rus+eng+chi_sim",
    "rus",
    "eng",
    "chi_sim",
  ]);
  assert.deepEqual(recognizedLanguages.slice(4, 6), ["ell", "equ"]);
  assert.deepEqual(recognizedLanguages.slice(6, 10), [
    "rus+eng+chi_sim",
    "eng",
    "rus",
    "chi_sim",
  ]);
  assert.deepEqual(recognizedLanguages.slice(10, 12), ["ell", "equ"]);

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry learns numeric table segments", async () => {
  let activeLanguages = "rus+eng";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return { data: { text: "36 72 4 3" } };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});

  await lease.recognize(new Blob(["first"]));
  await lease.recognize(new Blob(["second"]));
  await lease.recognize(new Blob(["third"]));
  await lease.recognize(new Blob(["fourth"]));

  assert.deepEqual(recognizedLanguages.slice(0, 5), [
    "rus+eng",
    "rus",
    "eng",
    "ell",
    "equ",
  ]);
  assert.deepEqual(recognizedLanguages.slice(15, 17), ["rus+eng", "equ"]);

  await lease.release();
  await pool.releaseCached();
});

test("detailed OCR requests blocks and exposes word boxes", async () => {
  const outputs: Array<Record<string, boolean> | undefined> = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async recognize(_input, _options, output) {
      outputs.push(output);
      return {
        data: {
          text: "A B",
          blocks: [
            {
              paragraphs: [
                {
                  lines: [
                    {
                      words: [
                        {
                          text: "A",
                          confidence: 91,
                          bbox: { x0: 10, y0: 12, x1: 20, y1: 30 },
                        },
                        {
                          text: "B",
                          confidence: 92,
                          bbox: { x0: 80, y0: 12, x1: 90, y1: 30 },
                        },
                      ],
                    },
                  ],
                },
              ],
            },
          ],
        },
      };
    },
    async terminate() {},
  }));

  const lease = await pool.acquire(profile(), () => {});
  const result = await lease.recognizeDetailed(new Blob(["image"]));

  assert.deepEqual(outputs, [{ text: true, blocks: true }]);
  assert.equal(result.text, "A B");
  assert.deepEqual(result.words, [
    {
      text: "A",
      confidence: 91,
      bbox: { x0: 10, y0: 12, x1: 20, y1: 30 },
    },
    {
      text: "B",
      confidence: 92,
      bbox: { x0: 80, y0: 12, x1: 90, y1: 30 },
    },
  ]);

  await lease.release();
  await pool.releaseCached();
});

test("worker pool keeps concurrent progress callbacks isolated", async () => {
  const recognitions: ReturnType<typeof deferred>[] = [];
  const workerFactory = async (
    _languages: string,
    _oem: number,
    options: {
      logger?: (message: { status?: string; progress?: number }) => void;
    },
  ) => {
    const workerIndex = recognitions.length;
    const recognition = deferred();
    recognitions.push(recognition);

    return {
      async recognize() {
        options.logger?.({
          status: "recognizing text",
          progress: workerIndex === 0 ? 0.1 : 0.8,
        });
        await recognition.promise;
        return { data: { text: `text-${workerIndex}` } };
      },
      async terminate() {},
    };
  };

  const pool = new BrowserOcrWorkerPool(workerFactory);
  const firstMessages: string[] = [];
  const secondMessages: string[] = [];

  const firstLease = await pool.acquire(profile(), (message) => {
    firstMessages.push(message);
  });
  const firstResult = firstLease.recognize(new Blob(["first"]));

  const secondLease = await pool.acquire(profile(), (message) => {
    secondMessages.push(message);
  });
  const secondResult = secondLease.recognize(new Blob(["second"]));

  recognitions[1].resolve();
  recognitions[0].resolve();

  assert.equal(await firstResult, "text-0");
  assert.equal(await secondResult, "text-1");
  assert.deepEqual(firstMessages, ["Распознавание... 10%"]);
  assert.deepEqual(secondMessages, ["Распознавание... 80%"]);

  await firstLease.release();
  await secondLease.release();
  await pool.releaseCached();
});

test("cached worker updates progress callback between sequential leases", async () => {
  let run = 0;
  let createdWorkers = 0;
  const workerFactory = async (
    _languages: string,
    _oem: number,
    options: {
      logger?: (message: { status?: string; progress?: number }) => void;
    },
  ) => {
    createdWorkers += 1;

    return {
      async recognize() {
        run += 1;
        options.logger?.({
          status: "recognizing text",
          progress: run === 1 ? 0.25 : 0.5,
        });
        return { data: { text: `text-${run}` } };
      },
      async terminate() {},
    };
  };

  const pool = new BrowserOcrWorkerPool(workerFactory);
  const firstMessages: string[] = [];
  const secondMessages: string[] = [];

  const firstLease = await pool.acquire(profile(), (message) => {
    firstMessages.push(message);
  });
  assert.equal(await firstLease.recognize(new Blob(["first"])), "text-1");
  await firstLease.release();

  const secondLease = await pool.acquire(profile(), (message) => {
    secondMessages.push(message);
  });
  assert.equal(await secondLease.recognize(new Blob(["second"])), "text-2");
  await secondLease.release();

  assert.equal(createdWorkers, 1);
  assert.deepEqual(firstMessages, ["Распознавание... 25%"]);
  assert.deepEqual(secondMessages, ["Распознавание... 50%"]);

  await pool.releaseCached();
});
