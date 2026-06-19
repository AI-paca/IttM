import test from "node:test";
import assert from "node:assert/strict";
import { sanitizeDiagnostic } from "./diagnostics";

test("diagnostics redact tokens and zero-width-obfuscated card-like values", () => {
  const diagnostic = sanitizeDiagnostic(
    "Bearer abc.def token=secret 4567\u200B8901\u200B2345\u200B6789",
  );

  assert.doesNotMatch(diagnostic, /abc\.def|secret|4567|6789/);
  assert.match(diagnostic, /\[REDACTED/);
});

test("diagnostic redaction never mutates canonical extraction text", () => {
  const canonical = "Contract 4567890123456789";

  sanitizeDiagnostic(canonical);

  assert.equal(canonical, "Contract 4567890123456789");
});

test("diagnostic payloads are bounded before observability transport", () => {
  assert.equal(sanitizeDiagnostic("x".repeat(5000), 512).length, 512);
});
