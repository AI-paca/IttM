import test from "node:test";
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { toTesseractRecognizeInput } from "./tesseract-recognize-input";

test("Node Canvas document shim still converts blobs to worker buffers", async () => {
  const globals = globalThis as unknown as Record<string, unknown>;
  const previousDocument = globals.document;
  const previousWindow = globals.window;
  globals.document = {};
  delete globals.window;

  try {
    const result = await toTesseractRecognizeInput(new Blob(["image"]));
    assert.equal(Buffer.isBuffer(result), true);
  } finally {
    if (previousDocument === undefined) delete globals.document;
    else globals.document = previousDocument;
    if (previousWindow === undefined) delete globals.window;
    else globals.window = previousWindow;
  }
});

test("real browser globals keep Blob input intact", async () => {
  const globals = globalThis as unknown as Record<string, unknown>;
  const previousDocument = globals.document;
  const previousWindow = globals.window;
  globals.document = {};
  globals.window = {};
  const blob = new Blob(["image"]);

  try {
    assert.equal(await toTesseractRecognizeInput(blob), blob);
  } finally {
    if (previousDocument === undefined) delete globals.document;
    else globals.document = previousDocument;
    if (previousWindow === undefined) delete globals.window;
    else globals.window = previousWindow;
  }
});
