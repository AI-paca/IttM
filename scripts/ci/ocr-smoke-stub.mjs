#!/usr/bin/env node

import http from "node:http";

const port = Number(process.env.PORT || 8000);

function writeJson(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(payload));
}

function drainRequest(request) {
  return new Promise((resolve, reject) => {
    request.on("data", () => {});
    request.on("end", resolve);
    request.on("error", reject);
  });
}

const server = http.createServer(async (request, response) => {
  const url = new URL(
    request.url || "/",
    `http://${request.headers.host || "localhost"}`,
  );

  if (
    request.method === "GET" &&
    (url.pathname === "/health" ||
      url.pathname === "/readiness" ||
      url.pathname === "/v1/readiness")
  ) {
    writeJson(response, 200, {
      ok: true,
      ready: true,
      service: "ocr-smoke-stub",
    });
    return;
  }

  if (request.method === "GET" && url.pathname === "/v1/capabilities") {
    writeJson(response, 200, {
      engines: ["tesseract"],
      profiles: ["backend_raw"],
    });
    return;
  }

  if (
    request.method === "POST" &&
    (url.pathname === "/convert" || url.pathname === "/v1/convert")
  ) {
    await drainRequest(request);
    writeJson(response, 200, {
      markdown: "compose smoke ocr stub",
      meta: { pages: 1, engine: "smoke" },
    });
    return;
  }

  if (
    request.method === "POST" &&
    (url.pathname === "/convert/stream" ||
      url.pathname === "/v1/convert/stream")
  ) {
    await drainRequest(request);
    response.writeHead(200, { "content-type": "application/x-ndjson" });
    response.write(
      `${JSON.stringify({ type: "accepted", taskId: "compose-smoke" })}\n`,
    );
    response.write(
      `${JSON.stringify({ type: "progress", stage: "ocr", page: 1, percent: 100 })}\n`,
    );
    response.write(
      `${JSON.stringify({ type: "page", page: 1, markdown: "compose smoke ocr stub" })}\n`,
    );
    response.end(
      `${JSON.stringify({ type: "complete", meta: { pages: 1, engine: "smoke" } })}\n`,
    );
    return;
  }

  writeJson(response, 404, { error: "not found" });
});

server.listen(port, "0.0.0.0", () => {
  process.stdout.write(`OCR smoke stub listening on ${port}\n`);
});
