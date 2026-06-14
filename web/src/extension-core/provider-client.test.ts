import test from "node:test";
import assert from "node:assert/strict";
import {
  ProviderClient,
  ProviderError,
  type ProviderTransport,
  type TokenProvider,
} from "./provider-client";

class FakeTokens implements TokenProvider {
  readonly calls: boolean[] = [];

  async getToken(forceRefresh: boolean): Promise<string> {
    this.calls.push(forceRefresh);
    return forceRefresh ? "fresh-token" : "stale-token";
  }
}

function jsonResponse(
  payload: Record<string, unknown>,
  status = 200,
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("captive portal HTML is rejected even with HTTP 200", async () => {
  const client = new ProviderClient(new FakeTokens(), {
    async send() {
      return new Response("<html><form>login</form></html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      });
    },
  });

  await assert.rejects(
    () => client.save("document"),
    (error: unknown) =>
      error instanceof ProviderError && error.code === "INVALID_RESPONSE",
  );
});

test("slow provider requests are aborted by a bounded timeout", async () => {
  const client = new ProviderClient(
    new FakeTokens(),
    {
      async send(_token, _payload, signal) {
        await new Promise<void>((_resolve, reject) => {
          signal.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
        return jsonResponse({});
      },
    },
    { timeoutMs: 5 },
  );

  await assert.rejects(
    () => client.save("document"),
    (error: unknown) =>
      error instanceof ProviderError && error.code === "TIMEOUT",
  );
});

test("expired OAuth token is refreshed exactly once after HTTP 401", async () => {
  const tokens = new FakeTokens();
  const observedTokens: string[] = [];
  const client = new ProviderClient(tokens, {
    async send(token) {
      observedTokens.push(token);
      return token === "stale-token"
        ? jsonResponse({ error: "expired" }, 401)
        : jsonResponse({ id: "file-1" });
    },
  });

  assert.deepEqual(await client.save("document"), { id: "file-1" });
  assert.deepEqual(tokens.calls, [false, true]);
  assert.deepEqual(observedTokens, ["stale-token", "fresh-token"]);
});

test("revoked access returns a typed non-retryable error", async () => {
  const client = new ProviderClient(new FakeTokens(), {
    async send() {
      return jsonResponse({ error: "revoked" }, 403);
    },
  });

  await assert.rejects(
    () => client.save("document"),
    (error: unknown) =>
      error instanceof ProviderError &&
      error.code === "FORBIDDEN" &&
      error.retryable === false,
  );
});

test("network failures open and later recover the provider circuit", async () => {
  let now = 1000;
  let calls = 0;
  const transport: ProviderTransport = {
    async send() {
      calls += 1;
      if (calls <= 2) throw new Error("offline");
      return jsonResponse({ id: "recovered" });
    },
  };
  const client = new ProviderClient(new FakeTokens(), transport, {
    failureThreshold: 2,
    cooldownMs: 100,
    now: () => now,
  });

  await assert.rejects(() => client.save("a"), /offline/);
  await assert.rejects(() => client.save("b"), /offline/);
  await assert.rejects(
    () => client.save("blocked"),
    (error: unknown) =>
      error instanceof ProviderError && error.code === "CIRCUIT_OPEN",
  );
  assert.equal(calls, 2);

  now += 101;
  assert.deepEqual(await client.save("retry"), { id: "recovered" });
});

test("rate limiting is classified without leaking document payload", async () => {
  const secret = "private document contents";
  const client = new ProviderClient(new FakeTokens(), {
    async send() {
      return jsonResponse({ error: "quota" }, 429);
    },
  });

  await assert.rejects(
    () => client.save(secret),
    (error: unknown) =>
      error instanceof ProviderError &&
      error.code === "RATE_LIMITED" &&
      !error.message.includes(secret),
  );
});
