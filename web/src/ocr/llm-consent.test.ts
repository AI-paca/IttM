import assert from "node:assert/strict";
import test from "node:test";
import {
  EXTERNAL_LLM_CONSENT_ERROR,
  runExternalLlmRequest,
} from "./llm-consent";

test("external LLM request is not started without explicit consent", async () => {
  let requestStarted = false;

  await assert.rejects(
    runExternalLlmRequest(false, async () => {
      requestStarted = true;
      return "sent";
    }),
    new RegExp(EXTERNAL_LLM_CONSENT_ERROR),
  );

  assert.equal(requestStarted, false);
});

test("external LLM request runs after consent", async () => {
  const result = await runExternalLlmRequest(true, async () => "sent");

  assert.equal(result, "sent");
});
