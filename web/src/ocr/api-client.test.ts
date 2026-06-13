import test from "node:test";
import assert from "node:assert/strict";
import {
  buildApiUrl,
  buildBackendGatewayCandidates,
  buildOllamaGenerateUrl,
  executeBackendOcrStreaming,
  isOllamaBaseUrl,
  normalizePlatformError,
  parseGatewayUrlList,
  parsePlatformError,
  readBackendOcrStream,
  readJsonOrThrow,
} from "./api-client";

test("normalizePlatformError preserves messages from browser error objects", () => {
  const error = normalizePlatformError({
    message: "Worker script failed to load",
  });

  assert.equal(error.message, "Worker script failed to load");
});

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

test("Ollama helpers detect and build direct generate endpoints", () => {
  assert.equal(isOllamaBaseUrl("http://localhost:11434"), true);
  assert.equal(isOllamaBaseUrl("https://edge.example"), false);
  assert.equal(
    buildOllamaGenerateUrl("http://localhost:11434"),
    "http://localhost:11434/api/generate",
  );
  assert.equal(
    buildOllamaGenerateUrl("http://localhost:11434/api"),
    "http://localhost:11434/api/generate",
  );
  assert.equal(
    buildOllamaGenerateUrl("http://localhost:11434/api/generate"),
    "http://localhost:11434/api/generate",
  );
});

test("parseGatewayUrlList handles comma/newline lists and dedupes", () => {
  assert.deepEqual(
    parseGatewayUrlList("https://edge.example/api,\n https://local.example/ "),
    ["https://edge.example/api", "https://local.example"],
  );
  assert.deepEqual(
    parseGatewayUrlList(" https://edge.example/ , https://edge.example"),
    ["https://edge.example"],
  );
});

test("buildBackendGatewayCandidates orders cloud, custom and local fallback", () => {
  assert.deepEqual(
    buildBackendGatewayCandidates({
      cloudBaseUrls: ["https://edge.example"],
      customBaseUrl: "https://custom.example/",
      includeLocal: true,
    }),
    [
      { label: "Cloud OCR", baseUrl: "https://edge.example" },
      { label: "Custom Gateway", baseUrl: "https://custom.example" },
      { label: "Local Gateway", baseUrl: "" },
    ],
  );
});

test("backend OCR uploads the original File without browser-side decoding", async () => {
  const originalFetch = globalThis.fetch;
  let arrayBufferCalls = 0;

  class GuardedFile extends File {
    override async arrayBuffer(): Promise<ArrayBuffer> {
      arrayBufferCalls += 1;
      throw new Error("local backend mode must not decode the file in browser");
    }
  }

  const file = new GuardedFile(["local payload"], "sample.png", {
    type: "image/png",
  });

  globalThis.fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ) => {
    assert.equal(init?.method, "POST");
    assert.ok(init?.body instanceof FormData);
    assert.equal(init.body.get("file"), file);

    return new Response(
      '{"type":"complete","meta":{"engine":"tesseract","pages":1}}\n',
      {
        headers: { "content-type": "application/x-ndjson" },
      },
    );
  };

  try {
    const result = await executeBackendOcrStreaming(
      file,
      "/api/convert/stream?engine_type=tesseract",
      { current: true },
    );

    assert.equal(arrayBufferCalls, 0);
    assert.deepEqual(result.meta, { engine: "tesseract", pages: 1 });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("readBackendOcrStream emits pages as NDJSON chunks arrive", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('{"type":"page","page":1,"markdown":"first"}\n{"type"'),
      );
      controller.enqueue(
        encoder.encode(
          ':"page","page":2,"markdown":"second"}\n{"type":"complete","meta":{"pages":2}}\n',
        ),
      );
      controller.close();
    },
  });
  const chunks: Array<[string, number | undefined]> = [];
  const progress: string[] = [];

  const result = await readBackendOcrStream(
    new Response(body, {
      headers: { "content-type": "application/x-ndjson" },
    }),
    { current: true },
    (message) => progress.push(message),
    (text, page) => chunks.push([text, page]),
  );

  assert.equal(result.markdown, "first\n\n---\n\nsecond");
  assert.deepEqual(result.meta, { pages: 2 });
  assert.deepEqual(chunks, [
    ["first\n\n---\n\n", 1],
    ["second\n\n---\n\n", 2],
  ]);
  assert.deepEqual(progress, [
    "Получена страница 1...",
    "Получена страница 2...",
  ]);
});

test("readBackendOcrStream reports backend error events", async () => {
  const response = new Response('{"type":"error","detail":"page 3 failed"}\n', {
    headers: { "content-type": "application/x-ndjson" },
  });

  await assert.rejects(
    () => readBackendOcrStream(response, { current: true }),
    /page 3 failed/,
  );
});

test("readBackendOcrStream rejects truncated responses", async () => {
  const response = new Response(
    '{"type":"page","page":1,"markdown":"partial"}\n',
    {
      headers: { "content-type": "application/x-ndjson" },
    },
  );

  await assert.rejects(
    () => readBackendOcrStream(response, { current: true }),
    /до события complete/,
  );
});
