import test from "node:test";
import assert from "node:assert/strict";
import {
  HeadlessClientError,
  HeadlessExtractionClient,
} from "./extraction-client";

test("headless client emits progressive page events from split NDJSON", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          '{"type":"accepted","taskId":"task-1"}\n' +
            '{"type":"progress","stage":"ocr","percent":50}\n' +
            '{"type":"page","page":1,"markdown":"first"}\n{"ty',
        ),
      );
      controller.enqueue(
        encoder.encode(
          'pe":"page","page":2,"markdown":"second"}\n' +
            '{"type":"warning","code":"LOW_CONFIDENCE","message":"check"}\n' +
            '{"type":"complete","meta":{"pages":2}}\n',
        ),
      );
      controller.close();
    },
  });
  const events: string[] = [];
  const client = new HeadlessExtractionClient(async () => {
    return new Response(body, {
      headers: { "content-type": "application/x-ndjson" },
    });
  });

  const result = await client.extract(
    new File(["x"], "sample.png", { type: "image/png" }),
    "http://localhost/api/convert/stream",
    { onEvent: (event) => events.push(event.type) },
  );

  assert.equal(result.markdown, "first\n\n---\n\nsecond");
  assert.deepEqual(result.meta, { pages: 2 });
  assert.deepEqual(events, [
    "accepted",
    "progress",
    "page",
    "page",
    "warning",
    "complete",
  ]);
});

test("headless client supports the legacy JSON contract", async () => {
  const client = new HeadlessExtractionClient(async () => {
    return new Response(
      JSON.stringify({
        markdown: "legacy",
        meta: { engine: "tesseract" },
      }),
      { headers: { "content-type": "application/json" } },
    );
  });

  assert.deepEqual(
    await client.extract(
      new File(["x"], "sample.png"),
      "http://localhost/api/convert",
    ),
    { markdown: "legacy", meta: { engine: "tesseract" } },
  );
});

test("headless client supports sync text task responses", async () => {
  const client = new HeadlessExtractionClient(async (_input, init) => {
    assert.equal(
      (init?.headers as Record<string, string> | undefined)?.accept,
      "text/plain",
    );
    return new Response("plain result", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  });

  assert.deepEqual(
    await client.extract(
      new File(["x"], "sample.png"),
      "http://localhost/api/extract/text",
      { accept: "text/plain" },
    ),
    { markdown: "plain result", meta: {} },
  );
});

test("headless client preserves non-ok text error bodies and status", async () => {
  const client = new HeadlessExtractionClient(async () => {
    return new Response("error: WORKER_FAILED: backend unavailable", {
      status: 502,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  });

  await assert.rejects(
    () =>
      client.extract(
        new File(["x"], "sample.png"),
        "http://localhost/api/extract/text",
      ),
    (error: unknown) =>
      error instanceof HeadlessClientError &&
      error.status === 502 &&
      error.message === "error: WORKER_FAILED: backend unavailable",
  );
});

test("headless client marks failures after a page as partial", async () => {
  const client = new HeadlessExtractionClient(async () => {
    return new Response(
      '{"type":"page","page":1,"markdown":"safe"}\n' +
        '{"type":"error","detail":"worker crashed"}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );
  });

  await assert.rejects(
    () =>
      client.extract(
        new File(["x"], "sample.png"),
        "http://localhost/api/convert/stream",
      ),
    (error: unknown) =>
      error instanceof HeadlessClientError &&
      error.partial &&
      /worker crashed/.test(error.message),
  );
});

test("headless client forwards AbortSignal to transport", async () => {
  const controller = new AbortController();
  let observedSignal: AbortSignal | null | undefined;
  const client = new HeadlessExtractionClient(async (_input, init) => {
    observedSignal = init?.signal;
    return new Response('{"type":"complete","meta":{}}\n', {
      headers: { "content-type": "application/x-ndjson" },
    });
  });

  await client.extract(
    new File(["x"], "sample.png"),
    "http://localhost/api/convert/stream",
    { signal: controller.signal },
  );

  assert.equal(observedSignal, controller.signal);
});
