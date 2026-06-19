import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { IncomingMessage, ServerResponse } from "node:http";
import { readFile } from "node:fs/promises";
import { send_web_response, to_web_request } from "../../../server";

test("Node gateway adapts uploads through the standard backpressure stream", async () => {
  const incoming = new PassThrough() as PassThrough & IncomingMessage;
  incoming.method = "POST";
  incoming.url = "/api/convert/stream";
  incoming.headers = {
    host: "localhost:3000",
    "content-type": "application/octet-stream",
  };

  const request = await to_web_request(incoming);
  incoming.write("first");
  incoming.end("-second");

  assert.equal(request.method, "POST");
  assert.equal(await request.text(), "first-second");
});

test("nginx proxies direct JSON and streaming compatibility routes", async () => {
  const config = await readFile("gateway/nginx.conf", "utf8");

  assert.match(config, /location ~ \^\/convert\(\?:\/stream\)\?\$/);
  assert.match(config, /proxy_request_buffering off;/);
});

test("Node gateway waits for response drain before reading the next chunk", async () => {
  class BackpressuredResponse extends EventEmitter {
    statusCode = 0;
    statusMessage = "";
    writes: Uint8Array[] = [];
    ended = false;
    headers = new Map<string, string | number | readonly string[]>();

    setHeader(name: string, value: string | number | readonly string[]) {
      this.headers.set(name, value);
      return this;
    }

    write(value: Uint8Array) {
      this.writes.push(value);
      return this.writes.length > 1;
    }

    end() {
      this.ended = true;
      return this;
    }
  }

  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode("first"));
      controller.enqueue(encoder.encode("second"));
      controller.close();
    },
  });
  const response = new BackpressuredResponse();
  const sendPromise = send_web_response(
    response as unknown as ServerResponse,
    new Response(body),
  );

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(response.writes.length, 1);
  assert.equal(response.ended, false);

  response.emit("drain");
  await sendPromise;

  assert.deepEqual(
    response.writes.map((chunk) => new TextDecoder().decode(chunk)),
    ["first", "second"],
  );
  assert.equal(response.ended, true);
});

test("Node gateway cancels a web response body after the client disconnects", async () => {
  class DisconnectedResponse extends EventEmitter {
    statusCode = 0;
    statusMessage = "";
    writes = 0;
    ended = false;

    setHeader() {
      return this;
    }

    write() {
      this.writes += 1;
      return true;
    }

    end() {
      this.ended = true;
      return this;
    }
  }

  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("first"));
    },
    cancel() {
      cancelled = true;
    },
  });
  const response = new DisconnectedResponse();
  const sendPromise = send_web_response(
    response as unknown as ServerResponse,
    new Response(body),
  );

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(response.writes, 1);
  response.emit("close");

  const outcome = await Promise.race([
    sendPromise.then(() => "completed"),
    new Promise<string>((resolve) =>
      setTimeout(() => resolve("timed-out"), 50),
    ),
  ]);
  assert.equal(outcome, "completed");
  assert.equal(cancelled, true);
  assert.equal(response.ended, false);
});
