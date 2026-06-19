import assert from "node:assert/strict";
import test from "node:test";
import { pdfJsDocumentOptions } from "./pdfjs-options";

test("PDF.js document options preserve data and point decoders at the app base", () => {
  const data = new ArrayBuffer(1);

  assert.deepEqual(pdfJsDocumentOptions(data, "/"), {
    data,
    wasmUrl: "/vendor/pdfjs/wasm/",
  });
  assert.deepEqual(pdfJsDocumentOptions(data, "/IttM"), {
    data,
    wasmUrl: "/IttM/vendor/pdfjs/wasm/",
  });
});
