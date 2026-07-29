import assert from "node:assert/strict";
import test from "node:test";
import { resolveRuntimeMode } from "./runtime-mode";

test("runtime mode enables browser pipeline only for explicit lite builds", () => {
  assert.equal(resolveRuntimeMode("lite"), "lite");
  assert.equal(resolveRuntimeMode("backend"), "backend");
  assert.equal(resolveRuntimeMode(undefined), "backend");
  assert.equal(resolveRuntimeMode("unexpected"), "backend");
});
