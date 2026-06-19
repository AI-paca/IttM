import test from "node:test";
import assert from "node:assert/strict";
import {
  isTokenFresh,
  loadSelectorConfig,
  resolveFeatureVariant,
  validateSelectorConfig,
  type SelectorConfig,
} from "./config";

const builtIn: SelectorConfig = {
  version: 1,
  selectors: { messages: '[data-testid="conversation-turn"]' },
};

test("missing or poisoned feature flags use a deterministic fallback", () => {
  const allowed = ["control", "compact"] as const;

  assert.equal(resolveFeatureVariant("compact", allowed, "control"), "compact");
  assert.equal(resolveFeatureVariant("NULL", allowed, "control"), "control");
  assert.equal(resolveFeatureVariant(null, allowed, "control"), "control");
});

test("remote selector outage falls back to the packaged configuration", async () => {
  assert.equal(
    await loadSelectorConfig(async () => {
      throw new Error("502 Bad Gateway");
    }, builtIn),
    builtIn,
  );
});

test("invalid selectors and executable-looking config are rejected", () => {
  assert.throws(
    () =>
      validateSelectorConfig({
        version: 2,
        selectors: { messages: "div>>script" },
      }),
    /Invalid selector/,
  );
  assert.throws(
    () =>
      validateSelectorConfig({
        version: 2,
        selectors: { messages: "javascript:alert(1)" },
      }),
    /Invalid selector/,
  );
});

test("valid data-only selectors are accepted without executable code", () => {
  assert.deepEqual(
    validateSelectorConfig({
      version: 2,
      selectors: {
        messages: '[data-testid="conversation-turn"]',
        toolbar: "main > div.toolbar",
      },
    }),
    {
      version: 2,
      selectors: {
        messages: '[data-testid="conversation-turn"]',
        toolbar: "main > div.toolbar",
      },
    },
  );
});

test("token expiry checks remain stable under spoofed past and future clocks", () => {
  const expiresAt = Date.UTC(2030, 0, 1);

  assert.equal(isTokenFresh(expiresAt, Date.UTC(2002, 0, 1)), true);
  assert.equal(isTokenFresh(expiresAt, Date.UTC(2100, 0, 1)), false);
  assert.equal(isTokenFresh(Number.NaN, Date.now()), false);
});
