import test from "node:test";
import assert from "node:assert/strict";
import { create_node_app } from "./node";
import type { Env } from "../domain/types";

const env: Env = {
  PORT: "0",
  OCR_URL: "http://ocr.local:8000",
};

test("node adapter wires static files through express.static between API and SPA layers", () => {
  const app = create_node_app(env, { distRoot: process.cwd() });
  const stackNames = app.router.stack.map(
    (layer: { name?: string }) => layer.name,
  );

  assert.deepEqual(stackNames, [
    "<anonymous>",
    "serveStatic",
    "serveStatic",
    "<anonymous>",
  ]);
});
