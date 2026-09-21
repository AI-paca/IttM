import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { discoverTests } from "./run-unit-tests.mjs";

test("discovers root and deeply nested tests without including OCR quality entrypoints", () => {
  const root = mkdtempSync(join(tmpdir(), "ittm-test-discovery-"));
  try {
    mkdirSync(join(root, "ui/theme"), { recursive: true });
    for (const name of [
      "runtime-mode.test.ts",
      "ui/theme/palettes.test.ts",
      "runner.test.mjs",
      "browser-engine.ocr-test.ts",
      "helper.ts",
    ]) {
      writeFileSync(join(root, name), "");
    }
    assert.deepEqual(discoverTests(root), [
      join(root, "runner.test.mjs"),
      join(root, "runtime-mode.test.ts"),
      join(root, "ui/theme/palettes.test.ts"),
    ]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
