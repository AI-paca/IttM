import test from "node:test";
import assert from "node:assert/strict";
import { HeadlessExtractionClient } from "./extraction-client";
import { runCli, type CliIo } from "./run";

function fakeIo() {
  const stdout: string[] = [];
  const stderr: string[] = [];
  const io: CliIo = {
    stdout: (value) => stdout.push(value),
    stderr: (value) => stderr.push(value),
    async readStdin() {
      return new TextEncoder().encode("stdin image");
    },
  };
  return { io, stdout, stderr };
}

test("CLI reads stdin and writes pages progressively", async () => {
  const { io, stdout, stderr } = fakeIo();
  let calledUrl = "";
  const client = new HeadlessExtractionClient(async (input, init) => {
    calledUrl = String(input);
    assert.equal(
      (init?.headers as Record<string, string> | undefined)?.accept,
      "application/x-ndjson",
    );
    return new Response(
      '{"type":"accepted","taskId":"task-1"}\n' +
        '{"type":"page","page":1,"markdown":"first"}\n' +
        '{"type":"page","page":2,"markdown":"second"}\n' +
        '{"type":"complete","meta":{"pages":2}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );
  });

  assert.equal(await runCli(["-", "--stream"], io, client), 0);
  assert.equal(calledUrl, "http://localhost:3000/api/tasks?sync=events");
  assert.deepEqual(stdout, ["first", "second"]);
  assert.deepEqual(stderr, []);
});

test("CLI defaults to the literal text endpoint and writes stdin result", async () => {
  const { io, stdout, stderr } = fakeIo();
  let calledUrl = "";
  const client = new HeadlessExtractionClient(async (input, init) => {
    calledUrl = String(input);
    assert.equal(
      (init?.headers as Record<string, string> | undefined)?.accept,
      "text/plain",
    );
    return new Response("plain text", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  });

  assert.equal(await runCli(["-"], io, client), 0);
  assert.equal(calledUrl, "http://localhost:3000/api/extract/text");
  assert.deepEqual(stdout, ["plain text"]);
  assert.deepEqual(stderr, []);
});

test("CLI returns stable usage and input exit codes", async () => {
  const missing = fakeIo();
  assert.equal(await runCli([], missing.io), 2);
  assert.match(missing.stderr.join(""), /Usage/);

  const unreadable = fakeIo();
  assert.equal(
    await runCli(["/definitely/missing/file.png"], unreadable.io),
    3,
  );
  assert.match(unreadable.stderr.join(""), /Input error/);
});

test("CLI maps cancellation to conventional exit code 130", async () => {
  const { io } = fakeIo();
  const controller = new AbortController();
  controller.abort();
  const client = new HeadlessExtractionClient(async () => {
    throw new DOMException("Aborted", "AbortError");
  });

  assert.equal(await runCli(["-"], io, client, controller.signal), 130);
});

test("CLI endpoint override bypasses the default URL", async () => {
  const { io, stdout } = fakeIo();
  let calledUrl = "";
  const client = new HeadlessExtractionClient(async (input) => {
    calledUrl = String(input);
    return new Response("custom", {
      headers: { "content-type": "text/plain" },
    });
  });

  assert.equal(
    await runCli(["-", "--endpoint=http://ocr.example/custom"], io, client),
    0,
  );
  assert.equal(calledUrl, "http://ocr.example/custom");
  assert.deepEqual(stdout, ["custom"]);
});

test("CLI stream mode falls back to the legacy endpoint when task routes are absent", async () => {
  const { io, stdout } = fakeIo();
  const calledUrls: string[] = [];
  const client = new HeadlessExtractionClient(async (input) => {
    calledUrls.push(String(input));
    if (calledUrls.length === 1) {
      return new Response("not found", {
        status: 404,
        headers: { "content-type": "text/plain" },
      });
    }
    return new Response(
      '{"type":"page","page":1,"markdown":"legacy"}\n' +
        '{"type":"complete","meta":{}}\n',
      { headers: { "content-type": "application/x-ndjson" } },
    );
  });

  assert.equal(await runCli(["-", "--stream"], io, client), 0);
  assert.deepEqual(calledUrls, [
    "http://localhost:3000/api/tasks?sync=events",
    "http://localhost:3000/api/convert/stream",
  ]);
  assert.deepEqual(stdout, ["legacy"]);
});
