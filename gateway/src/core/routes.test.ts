import test from "node:test";
import assert from "node:assert/strict";
import { route } from "./routes";
import type { Env } from "../domain/types";

const env: Env = {
  PORT: "3000",
  OCR_URL: "http://ocr.local:8000",
};

test("route proxies /api/probe to backend /v1/probe", async () => {
  const originalFetch = globalThis.fetch;
  let calledUrl = "";

  globalThis.fetch = async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    calledUrl = String(input);
    assert.equal(init?.method, "POST");
    return new Response(JSON.stringify({ ok: true, cases: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  try {
    const response = await route(
      new Request("http://localhost/api/probe", {
        method: "POST",
        body: JSON.stringify({ modes: ["all"], engines: ["auto"] }),
      }),
      env,
    );

    assert.equal(response.status, 200);
    assert.equal(calledUrl, "http://ocr.local:8000/v1/probe");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("route returns explicit JSON for dead install-light route", async () => {
  const response = await route(
    new Request("http://localhost/api/install-light", { method: "POST" }),
    env,
  );
  const payload = await response.json();

  assert.equal(response.status, 501);
  assert.match(payload.error, /not implemented/);
});

test("route returns 405 for wrong method and 404 for unknown route", async () => {
  const methodResponse = await route(
    new Request("http://localhost/api/convert", { method: "GET" }),
    env,
  );
  const missingResponse = await route(
    new Request("http://localhost/api/nope"),
    env,
  );

  assert.equal(methodResponse.status, 405);
  assert.equal(missingResponse.status, 404);
});
