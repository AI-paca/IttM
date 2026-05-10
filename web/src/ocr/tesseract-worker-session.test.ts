import test from "node:test";
import assert from "node:assert/strict";
import type { BrowserOcrProfile } from "./browser-profile";
import { BrowserOcrWorkerPool } from "./tesseract-worker-session";

function profile(): BrowserOcrProfile {
  return {
    languages: "eng",
    cacheWorker: true,
    maxImagePixels: 1_000_000,
    maxDimension: 1000,
    pdfRenderScale: 1,
    reason: "unit-test",
  };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

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
