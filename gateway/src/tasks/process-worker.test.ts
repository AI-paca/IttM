import test from "node:test";
import assert from "node:assert/strict";
import { execPath } from "node:process";
import { ProcessWorkerExecutor, WorkerProcessError } from "./process-worker";
import type { ExtractionRequest, WorkerEventInput } from "./types";

const request: ExtractionRequest = {
  filename: "generated.png",
  engine: "tesseract",
};

function executorFor(source: string, timeoutMs = 1_000) {
  return new ProcessWorkerExecutor({
    command: execPath,
    args: ["--input-type=module", "--eval", source],
    timeoutMs,
  });
}

async function execute(
  executor: ProcessWorkerExecutor,
  controller = new AbortController(),
) {
  const events: WorkerEventInput[] = [];
  const result = executor.execute(request, {
    signal: controller.signal,
    emit: (event) => events.push(event),
  });
  return { events, result, controller };
}

test("process worker forwards progressive events and completes", async () => {
  const worker = await execute(
    executorFor(`
      process.stdin.resume();
      process.stdout.write('{"type":"progress","stage":"decode"}\\n');
      process.stdout.write('{"type":"page","page":1,"markdown":"safe"}\\n');
      process.stdout.write('{"type":"complete","meta":{"pages":1}}\\n');
    `),
  );

  assert.deepEqual(await worker.result, { pages: 1 });
  assert.deepEqual(
    worker.events.map((event) => event.type),
    ["progress", "page"],
  );
});

test("process worker rejects malformed or oversized protocol output", async () => {
  const malformed = await execute(
    executorFor(`process.stdout.write('not-json\\n')`),
  );
  await assert.rejects(
    malformed.result,
    (error: unknown) =>
      error instanceof WorkerProcessError && error.code === "WORKER_PROTOCOL",
  );

  const oversized = new ProcessWorkerExecutor({
    command: execPath,
    args: [
      "--input-type=module",
      "--eval",
      `process.stdout.write('x'.repeat(2048))`,
    ],
    maxProtocolLineBytes: 128,
  });
  await assert.rejects(
    (await execute(oversized)).result,
    (error: unknown) =>
      error instanceof WorkerProcessError && error.code === "WORKER_PROTOCOL",
  );
});

test("process worker preserves partial events before malformed protocol failure", async () => {
  const worker = await execute(
    executorFor(`
      process.stdout.write('{"type":"page","page":1,"markdown":"partial before fault"}\\n');
      process.stdout.write('{malformed-json\\n');
    `),
  );

  await assert.rejects(
    worker.result,
    (error: unknown) =>
      error instanceof WorkerProcessError &&
      error.code === "WORKER_PROTOCOL" &&
      !error.retryable,
  );
  assert.deepEqual(worker.events, [
    { type: "page", page: 1, markdown: "partial before fault" },
  ]);
});

test("process worker sends the extraction request over stdin as the boundary payload", async () => {
  const worker = await execute(
    executorFor(`
      let payload = '';
      process.stdin.setEncoding('utf8');
      process.stdin.on('data', (chunk) => { payload += chunk; });
      process.stdin.on('end', () => {
        const request = JSON.parse(payload);
        process.stdout.write(JSON.stringify({
          type: 'page',
          page: 1,
          markdown: request.filename + ':' + request.engine,
        }) + '\\n');
        process.stdout.write('{"type":"complete","meta":{"stdin":true}}\\n');
      });
    `),
  );

  assert.deepEqual(await worker.result, { stdin: true });
  assert.deepEqual(worker.events, [
    { type: "page", page: 1, markdown: "generated.png:tesseract" },
  ]);
});

test("process worker rejects stale or unknown protocol events", async () => {
  const stale = await execute(
    executorFor(`process.stdout.write('{"type":"stale","version":99}\\n')`),
  );

  await assert.rejects(
    stale.result,
    (error: unknown) =>
      error instanceof WorkerProcessError && error.code === "WORKER_PROTOCOL",
  );
});

test("process worker reports a retryable exit after partial output", async () => {
  const worker = await execute(
    executorFor(`
      process.stdout.write('{"type":"page","page":1,"markdown":"partial"}\\n');
      process.exit(7);
    `),
  );

  await assert.rejects(
    worker.result,
    (error: unknown) =>
      error instanceof WorkerProcessError &&
      error.code === "WORKER_EXIT" &&
      error.retryable,
  );
  assert.deepEqual(worker.events, [
    { type: "page", page: 1, markdown: "partial" },
  ]);
});

test("process worker enforces timeout and cancellation", async () => {
  const timed = await execute(
    executorFor(`process.stdin.resume(); setInterval(() => {}, 1000)`, 30),
  );
  await assert.rejects(
    timed.result,
    (error: unknown) =>
      error instanceof WorkerProcessError &&
      error.code === "WORKER_TIMEOUT" &&
      error.retryable,
  );

  const cancelled = await execute(
    executorFor(`process.stdin.resume(); setInterval(() => {}, 1000)`),
  );
  cancelled.controller.abort();
  await assert.rejects(
    cancelled.result,
    (error: unknown) =>
      error instanceof WorkerProcessError && error.code === "WORKER_ABORTED",
  );
});

test("process worker reports native-style crashes and bounds stderr", async () => {
  const crashed = await execute(
    new ProcessWorkerExecutor({
      command: execPath,
      args: [
        "--input-type=module",
        "--eval",
        `
          process.stderr.write('bounded'.repeat(1000) + 'SENSITIVE_TAIL');
          process.kill(process.pid, 'SIGKILL');
        `,
      ],
      maxStderrBytes: 64,
    }),
  );

  await assert.rejects(
    crashed.result,
    (error: unknown) =>
      error instanceof WorkerProcessError &&
      error.code === "WORKER_EXIT" &&
      error.retryable &&
      error.message.length < 256 &&
      /SIGKILL/.test(error.message) &&
      !/SENSITIVE_TAIL/.test(error.message),
  );
});

test("process worker preserves a reported retryability decision", async () => {
  const worker = await execute(
    executorFor(`
      process.stdout.write(
        '{"type":"error","code":"BAD_MEDIA","message":"invalid image","retryable":false}\\n'
      );
    `),
  );

  await assert.rejects(
    worker.result,
    (error: unknown) =>
      error instanceof WorkerProcessError &&
      error.code === "WORKER_REPORTED" &&
      !error.retryable &&
      /invalid image/.test(error.message),
  );
});
