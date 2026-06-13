import test from "node:test";
import assert from "node:assert/strict";
import { PassThrough } from "node:stream";
import type { IncomingMessage } from "node:http";
import { readFile } from "node:fs/promises";
import { to_web_request } from "../../../server";

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
