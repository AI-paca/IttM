import test from "node:test";
import assert from "node:assert/strict";
import { createBrowserOcrProfile } from "./browser-profile";

test("low-memory browser keeps the Tesseract worker between document pages", () => {
  const profile = createBrowserOcrProfile({
    backend: null,
    browser: { memory: 2, cores: 2 },
  });

  assert.equal(profile.reason, "low-memory-browser");
  assert.equal(profile.cacheWorker, true);
  assert.equal(profile.maxImagePixels, 4_000_000);
});
