import test from "node:test";
import assert from "node:assert/strict";
import { buildApiUrl, parsePlatformError, readJsonOrThrow } from "./api-client";

test("parsePlatformError extracts FastAPI detail from JSON", async () => {
  const response = new Response(
    JSON.stringify({ detail: "Failed to process PDF: broken" }),
    {
      status: 400,
      headers: { "content-type": "application/json" },
    },
  );

  const error = await parsePlatformError(response, "OCR API");

  assert.equal(error.status, 400);
  assert.match(error.message, /OCR API/);
  assert.match(error.message, /Failed to process PDF/);
});

test("parsePlatformError strips platform HTML wrappers", async () => {
  const response = new Response(
    "<html><head><title>502 Bad Gateway</title></head><body><h1>nginx</h1><script>x()</script></body></html>",
    {
      status: 502,
      headers: { "content-type": "text/html" },
    },
  );

  const error = await parsePlatformError(response, "Gateway");

  assert.equal(error.message, "Gateway: 502 Bad Gateway");
  assert.doesNotMatch(error.message, /<html|<script|<\/h1>/);
});

test("readJsonOrThrow turns backend error payload into exception", async () => {
  const response = new Response(JSON.stringify({ error: "Backend offline" }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

  await assert.rejects(
    () => readJsonOrThrow(response, "Diagnostics"),
    /Backend offline/,
  );
});

test("buildApiUrl supports relative and custom gateway URLs", () => {
  assert.equal(buildApiUrl("", "/api/convert"), "/api/convert");
  assert.equal(
    buildApiUrl("", "/api/convert", { engine_type: "tesseract" }),
    "/api/convert?engine_type=tesseract",
  );
  assert.equal(
    buildApiUrl("https://example.com", "/api/convert"),
    "https://example.com/api/convert",
  );
  assert.equal(
    buildApiUrl("https://example.com/api", "/api/convert"),
    "https://example.com/api/convert",
  );
  assert.equal(
    buildApiUrl("https://example.com/api/convert", "/api/convert", {
      engine_type: "easyocr",
    }),
    "https://example.com/api/convert?engine_type=easyocr",
  );
});
