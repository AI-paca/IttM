import test from "node:test";
import assert from "node:assert/strict";
import {
  hasAvailableLocalBackend,
  shouldIncludeLocalBackend,
} from "./source-availability";

test("reports backend availability from diagnostics", () => {
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

test("full runtime always tries its same-origin gateway", () => {
  assert.equal(shouldIncludeLocalBackend(null, false), true);
  assert.equal(
    shouldIncludeLocalBackend(
      { backend: null, error: "diagnostics are still unavailable" },
      false,
    ),
    true,
  );
});

test("lite runtime only adds a confirmed backend gateway", () => {
  assert.equal(shouldIncludeLocalBackend(null, true), false);
  assert.equal(
    shouldIncludeLocalBackend({ backend: { engine: "tesseract" } }, true),
    true,
  );
});
