import test from "node:test";
import assert from "node:assert/strict";
import { handle } from "./handle";
import type { Env } from "../domain/types";

const env: Env = {
  PORT: "3000",
  OCR_URL: "http://ocr.local:8000",
};

test("handle leaves non-API requests to adapter static serving", async () => {
  const response = await handle(new Request("http://localhost/IttM/"), env);

  assert.equal(response.status, 404);
  assert.match(response.headers.get("content-type") ?? "", /json/);
});

test("handle accepts direct legacy convert API route", async () => {
  const response = await handle(
    new Request("http://localhost/convert", { method: "GET" }),
    env,
  );

  assert.equal(response.status, 405);
});
