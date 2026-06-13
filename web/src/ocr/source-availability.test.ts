import test from "node:test";
import assert from "node:assert/strict";
import { hasAvailableLocalBackend } from "./source-availability";

test("auto OCR waits for backend diagnostics before uploading", () => {
  assert.equal(hasAvailableLocalBackend(null), false);
  assert.equal(
    hasAvailableLocalBackend({
      backend: { engine: "tesseract" },
    }),
    true,
  );
  assert.equal(
    hasAvailableLocalBackend({
      backend: { engine: "tesseract" },
      error: "backend probe failed",
    }),
    false,
  );
  assert.equal(
    hasAvailableLocalBackend({
      backend: null,
    }),
    false,
  );
});
