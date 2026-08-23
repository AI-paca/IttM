import test from "node:test";
import assert from "node:assert/strict";
import type { BrowserOcrProfile } from "./browser-profile";
import {
  BrowserOcrWorkerPool as ProductionBrowserOcrWorkerPool,
  normalizeAppBaseUrl,
} from "./tesseract-worker-session";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";

const fakeCoreLoader = async () => ({
  spanEvidenceScore(options: {
    ocrConfidenceMilli: number;
    scriptConsistencyMilli: number;
    contextConsistencyMilli: number;
    sourceAgreement: number;
    contradictions?: number;
  }): number {
    return (
      options.ocrConfidenceMilli * 35 +
      options.scriptConsistencyMilli * 20 +
      options.contextConsistencyMilli * 15 +
      Math.min(4, options.sourceAgreement) * 7_500 -
      Math.min(4, options.contradictions ?? 0) * 15_000
    );
  },
  shouldReplacePrimary(options: {
    primaryChars: number;
    fallbackChars: number;
    primaryTokens: number;
    retainedPrimaryTokens: number;
  }): boolean {
    if (options.primaryChars < 10) return options.fallbackChars >= 80;
    if (!options.primaryTokens) return false;
    return (
      options.fallbackChars >=
        Math.max(180, Math.ceil(options.primaryChars * 1.4)) &&
      options.retainedPrimaryTokens * 5 >= options.primaryTokens * 4
    );
  },
});

class BrowserOcrWorkerPool extends ProductionBrowserOcrWorkerPool {
  constructor(
    createWorkerFn?: ConstructorParameters<
      typeof ProductionBrowserOcrWorkerPool
    >[0],
  ) {
    super(createWorkerFn, fakeCoreLoader);
  }
}

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

async function withMockImageVariants<T>(run: () => Promise<T>): Promise<T> {
  const globalRecord = globalThis as unknown as Record<string, unknown>;
  const previousOffscreenCanvas = globalRecord.OffscreenCanvas;
  const previousCreateImageBitmap = globalRecord.createImageBitmap;

  class FakeOffscreenCanvas {
    data: Uint8ClampedArray;

    constructor(
      readonly width: number,
      readonly height: number,
    ) {
      this.data = new Uint8ClampedArray(width * height * 4);
    }

    getContext() {
      return {
        fillStyle: "",
        fillRect: () => {
          this.data.fill(255);
        },
        drawImage: (source: {
          pixels?: Uint8ClampedArray;
          data?: Uint8ClampedArray;
        }) => {
          const sourceData =
            source.pixels || source.data || new Uint8ClampedArray([255]);
          for (let index = 0; index < this.data.length; index += 1) {
            this.data[index] = sourceData[index % sourceData.length];
          }
        },
        getImageData: () => {
          return { data: new Uint8ClampedArray(this.data) };
        },
        putImageData: (imageData: { data: Uint8ClampedArray }) => {
          this.data = new Uint8ClampedArray(imageData.data);
        },
      };
    }

    async convertToBlob() {
      return new Blob([new Uint8Array([this.data[0] || 0])], {
        type: "image/png",
      });
    }
  }

  globalRecord.OffscreenCanvas = FakeOffscreenCanvas;
  globalRecord.createImageBitmap = async (blob: Blob) => {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    return {
      width: 3,
      height: 2,
      close() {},
      pixels: new Uint8ClampedArray(
        Array.from(
          { length: 24 },
          (_, index) =>
            (bytes[index % Math.max(1, bytes.length)] + index) & 255,
        ),
      ),
    };
  };

  try {
    return await run();
  } finally {
    if (previousOffscreenCanvas === undefined) {
      delete globalRecord.OffscreenCanvas;
    } else {
      globalRecord.OffscreenCanvas = previousOffscreenCanvas;
    }
    if (previousCreateImageBitmap === undefined) {
      delete globalRecord.createImageBitmap;
    } else {
      globalRecord.createImageBitmap = previousCreateImageBitmap;
    }
  }
}

function t9VariantProfile(): BrowserOcrProfile {
  return {
    ...profile(),
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "off",
  };
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
  const fallback =
    "SAMPLE fallback with enough observed characters to replace an empty primary OCR result safely";
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters(params) {
      parameters.push(params);
    },
    async recognize() {
      recognition += 1;
      return {
        data: {
          text: recognition === 1 ? "" : fallback,
        },
      };
    },
    async terminate() {},
  }));

  const lease = await pool.acquire(profile(), () => {});
  assert.equal(await lease.recognize(new Blob(["image"])), fallback);
  assert.deepEqual(parameters, [
    { tessedit_pageseg_mode: "6" },
    { tessedit_pageseg_mode: "7" },
  ]);
  await lease.release();
  await pool.releaseCached();
});

test("edge-word fallback cannot replace an empty primary with a tiny fragment", async () => {
  let recognition = 0;
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async recognize() {
      recognition += 1;
      return { data: { text: recognition === 1 ? "" : "SAMPLE" } };
    },
    async terminate() {},
  }));

  const lease = await pool.acquire(profile(), () => {});
  assert.equal(await lease.recognize(new Blob(["image"])), "");
  assert.equal(recognition, 2);
  await lease.release();
  await pool.releaseCached();
});

test("image variants are skipped when the original OCR is strong", async () => {
  await withMockImageVariants(async () => {
    let recognizeCalls = 0;
    const pool = new BrowserOcrWorkerPool(async () => ({
      async setParameters() {},
      async recognize() {
        recognizeCalls += 1;
        return { data: { text: "CLEAR TEXT SAMPLE", confidence: 88 } };
      },
      async terminate() {},
    }));

    const lease = await pool.acquire(t9VariantProfile(), () => {});
    const result = await lease.recognize(
      new Blob([new Uint8Array([1, 2, 3, 4])], { type: "image/png" }),
    );

    assert.equal(result, "CLEAR TEXT SAMPLE");
    assert.equal(recognizeCalls, 1);

    await lease.release();
    await pool.releaseCached();
  });
});

test("image variants prefer textual OCR over high-confidence garbage", async () => {
  await withMockImageVariants(async () => {
    let recognizeCalls = 0;
    const texts = ["", "#####", "CONFIDENT RESULT", "РЕС"];
    const confidences = [99, 100, 5, 100];
    const pool = new BrowserOcrWorkerPool(async () => ({
      async setParameters() {},
      async recognize() {
        const index = recognizeCalls;
        recognizeCalls += 1;
        return {
          data: {
            text: texts[index] ?? "",
            confidence: confidences[index] ?? 0,
          },
        };
      },
      async terminate() {},
    }));

    const lease = await pool.acquire(t9VariantProfile(), () => {});
    const result = await lease.recognize(
      new Blob([new Uint8Array([1, 2, 3, 4])], { type: "image/png" }),
    );

    assert.equal(result, "CONFIDENT RESULT");
    assert.equal(recognizeCalls, 4);

    await lease.release();
    await pool.releaseCached();
  });
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
      const confidence = activeLanguages === "eng" ? 90 : text ? 40 : 0;
      return { data: { text, confidence } };
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
  const secondPrimary = recognizedLanguages.indexOf("rus+eng+chi_sim", 1);
  assert.ok(secondPrimary > 0);
  assert.deepEqual(
    recognizedLanguages.slice(secondPrimary, secondPrimary + 4),
    ["rus+eng+chi_sim", "eng", "rus", "chi_sim"],
  );

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry learns numeric table segments", async () => {
  let activeLanguages = "rus+eng+equ";
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
    languages: "rus+eng+equ",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});

  await lease.recognize(new Blob(["first"]));
  await lease.recognize(new Blob(["second"]));
  await lease.recognize(new Blob(["third"]));
  await lease.recognize(new Blob(["fourth"]));

  assert.deepEqual(recognizedLanguages.slice(0, 4), [
    "rus+eng+equ",
    "rus",
    "eng",
    "equ",
  ]);
  const primaryIndexes = recognizedLanguages
    .map((languages, index) => (languages === "rus+eng+equ" ? index : -1))
    .filter((index) => index >= 0);
  const lastPrimary = primaryIndexes.at(-1) ?? -1;
  assert.ok(lastPrimary > 0);
  assert.equal(recognizedLanguages[lastPrimary + 1], "equ");

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry learns single numeric cells", async () => {
  let activeLanguages = "rus+eng+equ";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return { data: { text: "5" } };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng+equ",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});

  await lease.recognize(new Blob(["first"]));
  await lease.recognize(new Blob(["second"]));

  assert.deepEqual(recognizedLanguages.slice(0, 4), [
    "rus+eng+equ",
    "rus",
    "eng",
    "equ",
  ]);
  const secondPrimary = recognizedLanguages.indexOf("rus+eng+equ", 1);
  assert.ok(secondPrimary > 0);
  assert.equal(recognizedLanguages[secondPrimary + 1], "equ");

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer favors compact numeric/math text for math language", async () => {
  let activeLanguages = "rus+eng+equ";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return {
        data: {
          text: activeLanguages === "equ" ? "2/3" : "23",
        },
      };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng+equ",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});
  const result = await lease.recognize(new Blob(["compact"]));

  assert.equal(result, "2/3");
  assert.equal(recognizedLanguages[0], "rus+eng+equ");
  assert.equal(recognizedLanguages[3], "equ");

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer favors compact greek text for greek language", async () => {
  let activeLanguages = "rus+eng+ell";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return {
        data: {
          text: activeLanguages === "ell" ? "π" : "A",
        },
      };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng+ell",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});
  const result = await lease.recognize(new Blob(["compact"]));

  assert.equal(result, "π");
  assert.equal(recognizedLanguages[0], "rus+eng+ell");
  assert.equal(recognizedLanguages[3], "ell");

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry skips unavailable math candidates", async () => {
  let activeLanguages = "rus+eng";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return { data: { text: activeLanguages === "equ" ? "5" : "" } };
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

  assert.equal(await lease.recognize(new Blob(["digit"])), "");
  assert.equal(recognizedLanguages.includes("equ"), false);

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer language retry adds available math candidates", async () => {
  let activeLanguages = "rus+eng";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return { data: { text: activeLanguages === "equ" ? "5" : "" } };
    },
    async terminate() {},
  }));
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng",
    availableLanguages: ["eng", "rus", "equ"],
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const lease = await pool.acquire(t9Profile, () => {});

  assert.equal(await lease.recognize(new Blob(["digit"])), "5");
  assert.ok(recognizedLanguages.includes("equ"));

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer empty state can reject punctuation noise", async () => {
  let activeLanguages = "rus+eng";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return {
        data: {
          text: activeLanguages === "rus+eng" ? "..." : "|_';",
        },
      };
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

  assert.equal(await lease.recognize(new Blob(["emptyish"])), "");
  assert.equal(recognizedLanguages.includes("empty"), false);

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer empty state does not hide a single digit", async () => {
  let activeLanguages = "rus+eng";
  const recognizedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      activeLanguages = languages;
    },
    async recognize() {
      recognizedLanguages.push(activeLanguages);
      return { data: { text: "5" } };
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

  assert.equal(await lease.recognize(new Blob(["digit"])), "5");
  assert.equal(recognizedLanguages.includes("empty"), false);

  await lease.release();
  await pool.releaseCached();
});

test("small reviewer promoted empty state still yields later digits", async () => {
  let activeLanguages = "rus+eng";
  let phase: "emptyish" | "digit" = "emptyish";
  const reinitializedLanguages: string[] = [];
  const pool = new BrowserOcrWorkerPool(async () => ({
    async setParameters() {},
    async reinitialize(languages) {
      reinitializedLanguages.push(languages);
      activeLanguages = languages;
    },
    async recognize() {
      if (phase === "digit") {
        return { data: { text: "5" } };
      }
      return {
        data: {
          text: activeLanguages === "rus+eng" ? "..." : "|_';",
        },
      };
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

  assert.equal(await lease.recognize(new Blob(["emptyish"])), "");
  phase = "digit";
  assert.equal(await lease.recognize(new Blob(["digit"])), "5");
  assert.equal(reinitializedLanguages.includes("empty"), false);

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

test("separated blocks reuse one multilingual worker without language reinitialization", async () => {
  let createdWorkers = 0;
  let recognitions = 0;
  let reinitializations = 0;
  const parameters: Record<string, string>[] = [];
  const pool = new BrowserOcrWorkerPool(async () => {
    createdWorkers += 1;
    return {
      async setParameters(params) {
        parameters.push(params);
      },
      async reinitialize() {
        reinitializations += 1;
      },
      async recognize() {
        recognitions += 1;
        return { data: { text: `segment-${recognitions}` } };
      },
      async terminate() {},
    };
  });
  const t9Profile: BrowserOcrProfile = {
    ...profile(),
    languages: "rus+eng+chi_sim",
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
  };

  const firstLease = await pool.acquire(t9Profile, () => {});
  assert.equal(
    await firstLease.recognizeSeparatedBlock(new Blob(["first"]), "7"),
    "segment-1",
  );
  await firstLease.release();

  const secondLease = await pool.acquire(t9Profile, () => {});
  assert.equal(
    await secondLease.recognizeSeparatedBlock(new Blob(["second"]), "7"),
    "segment-2",
  );
  await secondLease.release();

  assert.equal(createdWorkers, 1);
  assert.equal(recognitions, 2);
  assert.equal(reinitializations, 0);
  assert.deepEqual(parameters, [
    { tessedit_pageseg_mode: "7" },
    { tessedit_pageseg_mode: "7" },
  ]);

  await pool.releaseCached();
});

test("worker cache key differentiates tiny reviewer table settings", async () => {
  let createdWorkers = 0;
  const workerFactory = async () => {
    createdWorkers += 1;
    return {
      async setParameters() {},
      async recognize() {
        return { data: { text: "text" } };
      },
      async terminate() {},
    };
  };

  const pool = new BrowserOcrWorkerPool(workerFactory);
  const defaultProfile = profile();
  const tinyProfile: BrowserOcrProfile = {
    ...defaultProfile,
    lexicalCorrection: "t9_small",
    ocrLanguageRetry: "t9_small",
    tableSlotBuilder: "recursive_gaps_v1",
  };

  const firstLease = await pool.acquire(defaultProfile, () => {});
  await firstLease.release();
  const secondLease = await pool.acquire(tinyProfile, () => {});
  await secondLease.release();

  assert.equal(createdWorkers, 2);
  await pool.releaseCached();
});
