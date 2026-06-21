import assert from "node:assert/strict";
import test from "node:test";

import worker from "./cloudflare-worker";

const baseEnv = {
  ORIGIN_URL: "https://origin.example",
  ALLOWED_ORIGINS: "https://app.example, https://admin.example/",
};

function optionsRequest(origin?: string): Request {
  return new Request("https://edge.example/api/health", {
    method: "OPTIONS",
    headers: origin ? { Origin: origin } : undefined,
  });
}

test("edge CORS omits wildcard for requests without an Origin header", async () => {
  const response = await worker.fetch(optionsRequest(), baseEnv);

  assert.equal(response.status, 204);
  assert.equal(response.headers.get("access-control-allow-origin"), null);
});

test("edge CORS echoes only explicit allowlisted origins", async () => {
  const allowed = await worker.fetch(
    optionsRequest("https://admin.example"),
    baseEnv,
  );
  const blocked = await worker.fetch(
    optionsRequest("https://evil.example"),
    baseEnv,
  );

  assert.equal(
    allowed.headers.get("access-control-allow-origin"),
    "https://admin.example",
  );
  assert.equal(blocked.headers.get("access-control-allow-origin"), null);
});

test("edge CORS rejects wildcard configuration", async () => {
  const response = await worker.fetch(optionsRequest("https://app.example"), {
    ...baseEnv,
    ALLOWED_ORIGINS: "*",
  });

  assert.equal(response.status, 500);
  assert.equal(response.headers.get("access-control-allow-origin"), null);
  assert.match(await response.text(), /wildcard CORS is disabled/);
});

test("extension origins require an explicit runtime flag", async () => {
  const origin = "chrome-extension://abcdef";
  const blocked = await worker.fetch(optionsRequest(origin), baseEnv);
  const allowed = await worker.fetch(optionsRequest(origin), {
    ...baseEnv,
    ALLOW_EXTENSION_ORIGINS: "1",
  });

  assert.equal(blocked.headers.get("access-control-allow-origin"), null);
  assert.equal(allowed.headers.get("access-control-allow-origin"), origin);
});
