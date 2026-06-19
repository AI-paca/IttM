import assert from "node:assert/strict";
import test from "node:test";
import { BROWSER_PIPELINE_PROFILES } from "./pipeline-config";
import type {
  ResizeWorkerCommand,
  ResizeWorkerResponse,
} from "./image-resize-protocol";
import {
  shouldTryProjectorSlideDewarp,
  shouldTryProjectedDocumentDewarp,
  streamImagesFromResizeWorker,
  type ResizeWorkerLike,
} from "./browser-image-preprocessor";
import type { BrowserOcrProfile } from "./browser-profile";

function profile(): BrowserOcrProfile {
  return {
    languages: "eng",
    cacheWorker: false,
    maxImagePixels: 1_000_000,
    maxDimension: 1000,
    pdfRenderScale: 1,
    reason: "stream-test",
    preprocessingProfile: "browser_tesseract_standard",
    imagePreprocessing: ["browser_resize", "ocr_border"],
    textRegionPsm: "6",
    denseGridFallback: true,
    denseGridTargetWidth: 3300,
    ocrBorderPixels: 10,
    edgeWordFallbackPsm: "7",
    edgeWordFallbackMinTokens: 1,
    layout: BROWSER_PIPELINE_PROFILES.browser_tesseract_standard.layout,
  };
}

test("projected dewarp skips long screenshots before contour detection", () => {
  assert.equal(shouldTryProjectedDocumentDewarp(1240, 27466), false);
  assert.equal(shouldTryProjectedDocumentDewarp(1240, 12700), false);
  assert.equal(shouldTryProjectedDocumentDewarp(960, 1280), true);
});

test("projector slide dewarp targets projector photo dimensions only", () => {
  assert.equal(shouldTryProjectorSlideDewarp(960, 1280), true);
  assert.equal(shouldTryProjectorSlideDewarp(1600, 900), false);
  assert.equal(shouldTryProjectorSlideDewarp(1240, 27466), false);
});

test("resize worker tiles are yielded before the complete message", async () => {
  let terminated = false;
  let created = false;
  const commands: string[] = [];
  const observed: string[] = [];
  const responses: ResizeWorkerResponse[] = [
    {
      type: "tile",
      index: 0,
      total: 2,
      blob: new Blob(["first"]),
    },
    {
      type: "tile",
      index: 1,
      total: 2,
      blob: new Blob(["second"]),
    },
    { type: "complete", total: 2 },
  ];
  const fakeWorker: ResizeWorkerLike = {
    onmessage: null,
    onerror: null,
    postMessage(command: ResizeWorkerCommand) {
      commands.push(command.type);
      queueMicrotask(() => {
        const emit = (data: ResizeWorkerResponse) =>
          fakeWorker.onmessage?.({
            data,
          } as MessageEvent<ResizeWorkerResponse>);
        if (command.type === "start") {
          emit({ type: "plan", total: 2 });
        }
        const response = responses.shift();
        if (response) emit(response);
      });
    },
    terminate() {
      terminated = true;
    },
  };
  const file = new File(["source"], "source.png", { type: "image/png" });
  const stream = streamImagesFromResizeWorker(file, profile(), () => {
    created = true;
    return fakeWorker;
  });
  assert.ok(stream);
  assert.equal(created, false);

  for await (const prepared of stream) {
    observed.push(
      `${prepared.index + 1}/${prepared.total}:${await prepared.input.text()}`,
    );
  }

  assert.deepEqual(observed, ["1/2:first", "2/2:second"]);
  assert.equal(created, true);
  assert.deepEqual(commands, ["start", "next", "next"]);
  assert.equal(terminated, true);
});

test("passthrough keeps the original file without encoding it", async () => {
  const fakeWorker: ResizeWorkerLike = {
    onmessage: null,
    onerror: null,
    postMessage(command) {
      if (command.type !== "start") return;
      queueMicrotask(() => {
        fakeWorker.onmessage?.({
          data: { type: "passthrough", total: 1 },
        } as MessageEvent<ResizeWorkerResponse>);
        fakeWorker.onmessage?.({
          data: { type: "complete", total: 1 },
        } as MessageEvent<ResizeWorkerResponse>);
      });
    },
    terminate() {},
  };
  const file = new File(["source"], "source.png", { type: "image/png" });
  const stream = streamImagesFromResizeWorker(
    file,
    profile(),
    () => fakeWorker,
  );
  assert.ok(stream);

  const prepared = [];
  for await (const item of stream) prepared.push(item);

  assert.equal(prepared.length, 1);
  assert.equal(prepared[0].input, file);
});
