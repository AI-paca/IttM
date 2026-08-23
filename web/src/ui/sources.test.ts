import assert from "node:assert/strict";
import test from "node:test";
import { SOURCES } from "./sources";

test("Browser Engine is available in every runtime build", () => {
  assert.ok(SOURCES.some((source) => source.id === "browser"));
});
