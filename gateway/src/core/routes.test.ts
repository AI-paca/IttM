import test from "node:test";
import assert from "node:assert/strict";
import { route } from "./routes";
import type { Env } from "../domain/types";
import { resetTaskApiForTests } from "../tasks/http-api";

const env: Env = {
  PORT: "3000",
  OCR_URL: "http://ocr.local:8000",
};

test("task API creates a task and exposes its state without breaking convert routes", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      '{"type":"page","page":1,"markdown":"hello"}\n' +
        '{"type":"complete","meta":{"pages":1,"engine":"tesseract"}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );

  try {
    const response = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1, 2, 3]),
      }),
      env,
    );
    const accepted = (await response.json()) as { taskId: string };

    assert.equal(response.status, 202);
    assert.equal(
      response.headers.get("location"),
      `/api/tasks/${accepted.taskId}`,
    );

    await new Promise((resolve) => setImmediate(resolve));
    const state = await route(
      new Request(`http://localhost/api/tasks/${accepted.taskId}`),
      env,
    );
    const payload = (await state.json()) as {
      state: string;
      result?: { markdown: string };
    };

    assert.equal(state.status, 200);
    assert.equal(payload.state, "completed");
    assert.equal(payload.result?.markdown, "hello");
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API streams events as NDJSON or SSE based on Accept", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      '{"type":"page","page":1,"markdown":"hello"}\n' +
        '{"type":"complete","meta":{"pages":1}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );

  try {
    const create = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      env,
    );
    const { taskId } = (await create.json()) as { taskId: string };

    const ndjson = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "application/x-ndjson" },
      }),
      env,
    );
    assert.equal(ndjson.headers.get("content-type"), "application/x-ndjson");
    assert.match(await ndjson.text(), /"type":"accepted"/);

    const sse = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "text/event-stream" },
      }),
      env,
    );
    assert.match(sse.headers.get("content-type") ?? "", /text\/event-stream/);
    assert.match(await sse.text(), /event: accepted/);
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API resumes event streams after Last-Event-ID without duplicating accepted", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      '{"type":"page","page":1,"markdown":"hello"}\n' +
        '{"type":"complete","meta":{"pages":1}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );

  try {
    const create = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      env,
    );
    const { taskId } = (await create.json()) as { taskId: string };
    await new Promise((resolve) => setImmediate(resolve));

    const response = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: {
          accept: "application/x-ndjson",
          "last-event-id": "0",
        },
      }),
      env,
    );
    const text = await response.text();

    assert.doesNotMatch(text, /"type":"accepted"/);
    assert.match(text, /"type":"page"/);
    assert.match(text, /"type":"complete"/);
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API lists in-memory tasks with state and limit filters", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  let releaseFirst!: () => void;
  let holdFirst = true;
  globalThis.fetch = async () => {
    if (holdFirst) {
      await new Promise<void>((resolve) => {
        releaseFirst = () => {
          holdFirst = false;
          resolve();
        };
      });
    }
    return new Response('{"type":"complete","meta":{}}\n', {
      headers: { "content-type": "application/x-ndjson" },
    });
  };

  try {
    const first = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      env,
    );
    await first.json();
    const second = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([2]),
      }),
      env,
    );
    const { taskId } = (await second.json()) as { taskId: string };

    const response = await route(
      new Request("http://localhost/api/tasks?state=queued&limit=1"),
      env,
    );
    const payload = (await response.json()) as {
      count: number;
      state: string;
      limit: number;
      tasks: Array<{ id: string; state: string }>;
    };

    assert.equal(response.status, 200);
    assert.equal(payload.count, 1);
    assert.equal(payload.state, "queued");
    assert.equal(payload.limit, 1);
    assert.equal(payload.tasks[0]?.id, taskId);
    assert.equal(payload.tasks[0]?.state, "queued");
    releaseFirst();
  } finally {
    if (holdFirst && releaseFirst) releaseFirst();
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API sync text returns 499 and aborts backend work after client disconnect", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  let backendSignal: AbortSignal | null | undefined;
  globalThis.fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ) => {
    backendSignal = init?.signal;
    await new Promise<void>((_resolve, reject) => {
      init?.signal?.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
    throw new Error("unreachable");
  };

  try {
    const pending = route(
      new Request("http://localhost/api/tasks?sync=text", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "text/plain" },
        body: new Uint8Array([1, 2, 3]),
        signal: controller.signal,
      }),
      env,
    );
    while (!backendSignal) {
      await new Promise((resolve) => setImmediate(resolve));
    }

    controller.abort();
    const response = await pending;

    assert.equal(response.status, 499);
    assert.equal(await response.text(), "");
    assert.equal(backendSignal.aborted, true);
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API delays cancellation after event stream disconnect", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  let backendSignal: AbortSignal | null | undefined;
  globalThis.fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ) => {
    backendSignal = init?.signal;
    await new Promise<void>((_resolve, reject) => {
      if (init?.signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      init?.signal?.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
    throw new Error("unreachable");
  };
  const testEnv: Env = { ...env, TASK_EVENTS_DISCONNECT_GRACE_MS: "50" };

  try {
    const create = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      testEnv,
    );
    const { taskId } = (await create.json()) as { taskId: string };
    await waitFor(() => Boolean(backendSignal));

    const events = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "application/x-ndjson" },
        signal: controller.signal,
      }),
      testEnv,
    );
    const reader = events.body?.getReader();
    assert.ok(reader);
    const first = await reader.read();
    assert.equal(first.done, false);
    assert.match(decode(first.value), /"type":"accepted"/);

    controller.abort();
    await delay(0);

    assert.equal(backendSignal?.aborted, false);
    assert.equal((await readTask(taskId, testEnv)).state, "running");

    await waitFor(async () => {
      const task = await readTask(taskId, testEnv);
      return task.state === "cancelled";
    });
    assert.equal(backendSignal?.aborted, true);
  } finally {
    controller.abort();
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API keeps event stream tasks alive when a watcher reconnects before grace expires", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  const firstController = new AbortController();
  const secondController = new AbortController();
  let backendSignal: AbortSignal | null | undefined;
  globalThis.fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ) => {
    backendSignal = init?.signal;
    await new Promise<void>((_resolve, reject) => {
      if (init?.signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      init?.signal?.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
    });
    throw new Error("unreachable");
  };
  const testEnv: Env = { ...env, TASK_EVENTS_DISCONNECT_GRACE_MS: "40" };

  try {
    const create = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      testEnv,
    );
    const { taskId } = (await create.json()) as { taskId: string };
    await waitFor(() => Boolean(backendSignal));

    const firstEvents = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "application/x-ndjson" },
        signal: firstController.signal,
      }),
      testEnv,
    );
    const firstReader = firstEvents.body?.getReader();
    assert.ok(firstReader);
    await firstReader.read();
    firstController.abort();

    const secondEvents = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "application/x-ndjson" },
        signal: secondController.signal,
      }),
      testEnv,
    );
    const secondReader = secondEvents.body?.getReader();
    assert.ok(secondReader);
    const resumed = await secondReader.read();
    assert.equal(resumed.done, false);
    assert.match(decode(resumed.value), /"type":"accepted"/);

    await delay(80);

    assert.equal(backendSignal?.aborted, false);
    assert.equal((await readTask(taskId, testEnv)).state, "running");

    secondController.abort();
    await route(
      new Request(`http://localhost/api/tasks/${taskId}/cancel`, {
        method: "POST",
      }),
      testEnv,
    );
    await waitFor(() => backendSignal?.aborted === true);
  } finally {
    firstController.abort();
    secondController.abort();
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API does not grace-cancel terminal tasks after event stream disconnect", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      '{"type":"page","page":1,"markdown":"done"}\n' +
        '{"type":"complete","meta":{"pages":1}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );
  const testEnv: Env = { ...env, TASK_EVENTS_DISCONNECT_GRACE_MS: "10" };
  const controller = new AbortController();

  try {
    const create = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      testEnv,
    );
    const { taskId } = (await create.json()) as { taskId: string };
    await waitFor(
      async () => (await readTask(taskId, testEnv)).state === "completed",
    );

    controller.abort();
    const events = await route(
      new Request(`http://localhost/api/tasks/${taskId}/events`, {
        headers: { accept: "application/x-ndjson" },
        signal: controller.signal,
      }),
      testEnv,
    );
    await events.text();
    await delay(30);

    const task = await readTask(taskId, testEnv);
    assert.equal(task.state, "completed");
    assert.equal(
      task.events.filter((event) => event.type === "error").length,
      0,
    );
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API supports queued cancellation", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  let release!: () => void;
  globalThis.fetch = async () => {
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    return new Response('{"type":"complete","meta":{}}\n', {
      headers: { "content-type": "application/x-ndjson" },
    });
  };

  try {
    const first = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([1]),
      }),
      env,
    );
    await first.json();
    const second = await route(
      new Request("http://localhost/api/tasks", {
        method: "POST",
        headers: { "content-type": "image/png", accept: "application/json" },
        body: new Uint8Array([2]),
      }),
      env,
    );
    const { taskId } = (await second.json()) as { taskId: string };

    const cancelled = await route(
      new Request(`http://localhost/api/tasks/${taskId}/cancel`, {
        method: "POST",
      }),
      env,
    );
    const payload = (await cancelled.json()) as {
      state: string;
      error?: { code: string };
    };

    assert.equal(cancelled.status, 200);
    assert.equal(payload.state, "cancelled");
    assert.equal(payload.error?.code, "CANCELLED");
    release();
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

test("task API sync text mode returns plain markdown for Hyprland curl pipelines", async () => {
  resetTaskApiForTests();
  const originalFetch = globalThis.fetch;
  let calledUrl = "";
  globalThis.fetch = async (input: string | URL | Request) => {
    calledUrl = String(input);
    return new Response(
      '{"type":"progress","stage":"decode"}\n' +
        '{"type":"page","page":1,"markdown":"copied text"}\n' +
        '{"type":"complete","meta":{"pages":1}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );
  };

  try {
    const response = await route(
      new Request(
        "http://localhost/api/tasks?sync=text&engine=auto&profile=tesseract",
        {
          method: "POST",
          headers: { "content-type": "image/png", accept: "text/plain" },
          body: new Uint8Array([1, 2, 3]),
        },
      ),
      env,
    );

    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type") ?? "", /text\/plain/);
    assert.equal(response.headers.get("content-length"), "11");
    assert.equal(await response.text(), "copied text");
    assert.match(
      calledUrl,
      /\/v1\/convert\/stream\?engine_type=auto&pipeline_profile=tesseract/,
    );
  } finally {
    globalThis.fetch = originalFetch;
    resetTaskApiForTests();
  }
});

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

test("route proxies streaming OCR without buffering the response", async () => {
  const originalFetch = globalThis.fetch;
  let calledUrl = "";
  let proxiedBody: BodyInit | null | undefined;

  globalThis.fetch = async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    calledUrl = String(input);
    assert.equal(init?.method, "POST");
    proxiedBody = init?.body;
    return new Response('{"type":"page","page":1,"markdown":"text"}\n', {
      headers: { "content-type": "application/x-ndjson" },
    });
  };

  try {
    const form = new FormData();
    form.append("file", new Blob(["x"]), "sample.png");
    const request = new Request(
      "http://localhost/api/convert/stream?engine_type=tesseract",
      {
        method: "POST",
        body: form,
      },
    );
    const response = await route(request, env);

    assert.equal(
      calledUrl,
      "http://ocr.local:8000/v1/convert/stream?engine_type=tesseract",
    );
    assert.equal(proxiedBody, request.body);
    assert.equal(response.headers.get("content-type"), "application/x-ndjson");
    assert.match(await response.text(), /"type":"page"/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("route forwards client disconnect signals to the OCR backend", async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  let forwardedSignal: AbortSignal | null | undefined;

  globalThis.fetch = async (
    _input: string | URL | Request,
    init?: RequestInit,
  ) => {
    forwardedSignal = init?.signal;
    return new Response('{"type":"complete","meta":{"pages":0}}\n', {
      headers: { "content-type": "application/x-ndjson" },
    });
  };

  try {
    const form = new FormData();
    form.append("file", new Blob(["x"]), "sample.png");
    const request = new Request("http://localhost/api/convert/stream", {
      method: "POST",
      body: form,
      signal: controller.signal,
    });

    await route(request, env);
    assert.equal(forwardedSignal, request.signal);
    controller.abort();
    assert.equal(forwardedSignal?.aborted, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("route preserves backend status and error body", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "worker unavailable" }), {
      status: 503,
      headers: { "content-type": "application/json" },
    });

  try {
    const form = new FormData();
    form.append("file", new Blob(["x"]), "sample.png");
    const response = await route(
      new Request("http://localhost/api/convert", {
        method: "POST",
        body: form,
      }),
      env,
    );

    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {
      detail: "worker unavailable",
    });
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

test("route includes OCR target in gateway fetch failures", async () => {
  const originalFetch = globalThis.fetch;
  const form = new FormData();
  form.append("file", new Blob(["x"], { type: "text/plain" }), "sample.txt");
  const error = new Error("fetch failed");
  (error as any).cause = {
    code: "ECONNREFUSED",
    message: "connect ECONNREFUSED",
  };

  globalThis.fetch = async () => {
    throw error;
  };

  try {
    const response = await route(
      new Request("http://localhost/api/convert?engine_type=tesseract", {
        method: "POST",
        body: form,
      }),
      env,
    );
    const payload = await response.json();

    assert.equal(response.status, 502);
    assert.match(payload.error, /http:\/\/ocr\.local:8000\/v1\/convert/);
    assert.match(payload.error, /ECONNREFUSED/);
  } finally {
    globalThis.fetch = originalFetch;
  }
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

async function readTask(
  taskId: string,
  testEnv: Env,
): Promise<{ state: string; events: Array<{ type: string }> }> {
  const response = await route(
    new Request(`http://localhost/api/tasks/${taskId}`),
    testEnv,
  );
  return (await response.json()) as {
    state: string;
    events: Array<{ type: string }>;
  };
}

async function waitFor(
  condition: () => boolean | Promise<boolean>,
  timeoutMs = 500,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() <= deadline) {
    if (await condition()) return;
    await delay(1);
  }
  assert.fail("Timed out waiting for condition.");
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function decode(value: Uint8Array | undefined): string {
  return new TextDecoder().decode(value);
}
