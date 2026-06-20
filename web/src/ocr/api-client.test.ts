import test from "node:test";
import assert from "node:assert/strict";
import {
  buildApiUrl,
  buildBackendGatewayCandidates,
  buildOllamaGenerateUrl,
  executeBackendOcrWithFallback,
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

test("readJsonOrThrow rejects captive portal HTML returned with HTTP 200", async () => {
  const response = new Response(
    "<html><body><form>Connect to Wi-Fi</form></body></html>",
    {
      status: 200,
      headers: { "content-type": "text/html" },
    },
  );

  await assert.rejects(
    () => readJsonOrThrow(response, "Diagnostics"),
    /ответ не является JSON/,
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

test("readBackendOcrStream tolerates backend progress and warning events", async () => {
  const response = new Response(
    [
      '{"type":"progress","stage":"ocr"}',
      '{"type":"progress","stage":"ocr","message":"Processing page 2","percent":50}',
      '{"type":"warning","code":"EMPTY_PAGE","message":"No text was recognized on page 2.","page":2}',
      '{"type":"page","page":1,"markdown":"first"}',
      '{"type":"complete","meta":{"pages":1}}',
    ].join("\n") + "\n",
    {
      headers: { "content-type": "application/x-ndjson" },
    },
  );
  const progress: string[] = [];

  const result = await readBackendOcrStream(
    response,
    { current: true },
    (message) => progress.push(message),
  );

  assert.equal(result.markdown, "first");
  assert.deepEqual(progress, [
    "Processing page 2",
    "No text was recognized on page 2.",
    "Получена страница 1...",
  ]);
  assert.deepEqual(result.meta, {
    pages: 1,
    warnings: [
      {
        code: "EMPTY_PAGE",
        message: "No text was recognized on page 2.",
        page: 2,
      },
    ],
  });
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

test("backend fallback stops after a partial streaming result", async () => {
  const originalFetch = globalThis.fetch;
  const encoder = new TextEncoder();
  const requestedUrls: string[] = [];
  const chunks: string[] = [];

  globalThis.fetch = async (input: string | URL | Request) => {
    requestedUrls.push(String(input));
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            '{"type":"page","page":1,"markdown":"first"}\n' +
              '{"type":"error","detail":"page 2 failed"}\n',
          ),
        );
        controller.close();
      },
    });
    return new Response(body, {
      headers: { "content-type": "application/x-ndjson" },
    });
  };

  try {
    await assert.rejects(
      () =>
        executeBackendOcrWithFallback(
          new File(["pdf"], "book.pdf", { type: "application/pdf" }),
          [
            { label: "First", baseUrl: "https://first.example" },
            { label: "Second", baseUrl: "https://second.example" },
          ],
          { current: true },
          undefined,
          undefined,
          (text) => chunks.push(text),
        ),
      (error: unknown) => {
        assert.equal(normalizePlatformError(error).partialResult, true);
        assert.match(normalizePlatformError(error).message, /page 2 failed/);
        return true;
      },
    );

    assert.deepEqual(requestedUrls, [
      "https://first.example/api/convert/stream",
    ]);
    assert.deepEqual(chunks, ["first\n\n---\n\n"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
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
    {
      message: /до события complete/,
      partialResult: true,
    },
  );
});

test("readBackendOcrStream cancels the reader after client cancellation", async () => {
  const active = { current: true };
  let cancelled = false;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('{"type":"page","page":1,"markdown":"first"}\n'),
      );
    },
    cancel() {
      cancelled = true;
    },
  });

  const result = await readBackendOcrStream(
    new Response(body, {
      headers: { "content-type": "application/x-ndjson" },
    }),
    active,
    undefined,
    () => {
      active.current = false;
    },
  );

  assert.equal(cancelled, true);
  assert.equal(result.markdown, "first");
});

test("readBackendOcrStream rejects unknown protocol events", async () => {
  const response = new Response('{"type":"version-99","payload":{}}\n', {
    headers: { "content-type": "application/x-ndjson" },
  });

  await assert.rejects(
    () => readBackendOcrStream(response, { current: true }),
    /неизвестное событие/,
  );
});

test("streaming API falls back to legacy JSON only for missing routes", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = async (input: string | URL | Request) => {
    urls.push(String(input));
    if (urls.length === 1) {
      return new Response("Not Found", { status: 404 });
    }
    return new Response(
      JSON.stringify({
        markdown: "legacy result",
        meta: { pages: 1 },
      }),
      { headers: { "content-type": "application/json" } },
    );
  };

  try {
    const chunks: string[] = [];
    const result = await executeBackendOcrStreaming(
      new File(["x"], "sample.png", { type: "image/png" }),
      "/api/convert/stream?engine_type=tesseract",
      { current: true },
      undefined,
      (chunk) => chunks.push(chunk),
    );

    assert.deepEqual(urls, [
      "/api/convert/stream?engine_type=tesseract",
      "/api/convert?engine_type=tesseract",
    ]);
    assert.equal(result.markdown, "legacy result");
    assert.deepEqual(chunks, ["legacy result"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("stream reader handles a million-character page without recursion", async () => {
  const markdown = "x".repeat(1_000_000);
  const response = new Response(
    `${JSON.stringify({ type: "page", page: 1, markdown })}\n` +
      '{"type":"complete","meta":{"pages":1}}\n',
    { headers: { "content-type": "application/x-ndjson" } },
  );

  const result = await readBackendOcrStream(response, { current: true });

  assert.equal(result.markdown.length, 1_000_000);
});
