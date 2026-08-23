import assert from "node:assert/strict";
import test from "node:test";

import {
  BrowserPipelineCore,
  capabilityBits,
  configureBrowserPipelineCoreUrl,
  loadBrowserPipelineCore,
  PIPELINE_STAGES,
} from "./pipeline-core";

function fakeCore() {
  return new BrowserPipelineCore({
    ittm_pipeline_abi_version: () => 4,
    ittm_pipeline_recipe_mask: (bits: number) => {
      const trusted = Boolean(bits & 1);
      const layout = Boolean(bits & 2);
      const markdown = Boolean(bits & 4);
      const retry = Boolean(bits & 8);
      let mask = 1 << 3;
      if (!layout) mask |= (1 << 0) | (1 << 1) | (1 << 2);
      if (!trusted && retry) mask |= 1 << 4;
      if (!trusted) mask |= 1 << 5;
      if (!markdown) mask |= (1 << 6) | (1 << 7);
      return mask;
    },
    ittm_sparse_add_signal: (code: number, signal: number) => code + signal,
    ittm_is_isolated_heading: () => 0,
    ittm_span_evidence_score: (confidence: number) => confidence,
    ittm_should_replace_primary: (_primary, fallback) => Number(fallback >= 80),
    ittm_should_drop_text_block: (
      _candidateChars,
      _existingChars,
      sharedTokens,
      candidateTokens,
      existingTokens,
      similarityMilli,
    ) =>
      Number(
        similarityMilli >= 880 || sharedTokens * 1_000 >= candidateTokens * 850,
      ),
  });
}

test("browser capability bits match Rust ABI", () => {
  assert.equal(capabilityBits({}), 8);
  assert.equal(
    capabilityBits({
      trustedText: true,
      providesLayout: true,
      providesMarkdown: true,
      needsLanguageRetry: false,
    }),
    7,
  );
});

test("trusted markdown bypasses local T9 and grammar", () => {
  const recipe = fakeCore().recipe({
    trustedText: true,
    providesLayout: true,
    providesMarkdown: true,
    needsLanguageRetry: false,
  });

  assert.deepEqual([...recipe], ["recognize_segments"]);
});

test("local OCR uses the complete shared recipe", () => {
  assert.deepEqual([...fakeCore().recipe({})], PIPELINE_STAGES);
});

test("browser adapter rejects an incompatible ABI", () => {
  assert.throws(
    () =>
      new BrowserPipelineCore({
        ittm_pipeline_abi_version: () => 1,
        ittm_pipeline_recipe_mask: () => 0,
        ittm_sparse_add_signal: () => 0,
        ittm_is_isolated_heading: () => 0,
        ittm_span_evidence_score: () => 0,
        ittm_should_replace_primary: () => 0,
        ittm_should_drop_text_block: () => 0,
      }),
    /Unsupported pipeline core ABI 1/,
  );
});

test("browser adapter maps raw sparse errors", () => {
  const core = new BrowserPipelineCore({
    ittm_pipeline_abi_version: () => 4,
    ittm_pipeline_recipe_mask: () => 0,
    ittm_sparse_add_signal: (_code: number, signal: number) =>
      signal === 1 ? -2 : -1,
    ittm_is_isolated_heading: () => 0,
    ittm_span_evidence_score: () => 0,
    ittm_should_replace_primary: () => 0,
    ittm_should_drop_text_block: () => 0,
  });

  assert.throws(() => core.addSparseSignal(3, 1), /Unknown sparse signal/);
  assert.throws(() => core.addSparseSignal(1, 3), /Unknown sparse code/);
});

test("browser adapter delegates heading classification to WASM", () => {
  const core = new BrowserPipelineCore({
    ittm_pipeline_abi_version: () => 4,
    ittm_pipeline_recipe_mask: () => 0,
    ittm_sparse_add_signal: () => 0,
    ittm_is_isolated_heading: (_rows, chars, _line, boundaries) =>
      Number(chars >= 6 && boundaries === 3),
    ittm_span_evidence_score: () => 0,
    ittm_should_replace_primary: () => 0,
    ittm_should_drop_text_block: () => 0,
  });

  assert.equal(
    core.isIsolatedHeading({
      runRows: 1,
      contentChars: 8,
      maxLineChars: 8,
      boundedAbove: true,
      boundedBelow: true,
    }),
    true,
  );
});

test("browser adapter delegates span evidence scoring to WASM", () => {
  const core = fakeCore();
  assert.equal(
    core.spanEvidenceScore({
      ocrConfidenceMilli: 725,
      scriptConsistencyMilli: 900,
      contextConsistencyMilli: 700,
      sourceAgreement: 2,
    }),
    725,
  );
  assert.throws(
    () =>
      core.spanEvidenceScore({
        ocrConfidenceMilli: -1,
        scriptConsistencyMilli: 900,
        contextConsistencyMilli: 700,
        sourceAgreement: 2,
      }),
    /non-negative integers/,
  );
});

test("browser adapter delegates fallback replacement to WASM", () => {
  const core = fakeCore();

  assert.equal(
    core.shouldReplacePrimary({
      primaryChars: 2,
      fallbackChars: 80,
      primaryTokens: 1,
      retainedPrimaryTokens: 1,
    }),
    true,
  );
  assert.throws(
    () =>
      core.shouldReplacePrimary({
        primaryChars: -1,
        fallbackChars: 80,
        primaryTokens: 1,
        retainedPrimaryTokens: 1,
      }),
    /non-negative integers/,
  );
});

test("browser adapter delegates text block deduplication to WASM", () => {
  const core = fakeCore();

  assert.equal(
    core.shouldDropTextBlock({
      candidateChars: 100,
      existingChars: 100,
      sharedTokens: 9,
      candidateTokens: 10,
      existingTokens: 10,
      similarityMilli: 0,
    }),
    true,
  );
  assert.equal(
    core.shouldDropTextBlock({
      candidateChars: 100,
      existingChars: 100,
      sharedTokens: 8,
      candidateTokens: 10,
      existingTokens: 10,
      similarityMilli: 0,
    }),
    false,
  );
});

test("browser pipeline core accepts an explicit URL for non-browser harnesses", async () => {
  const bytes = await WebAssembly.compile(
    new Uint8Array([0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00]),
  );
  assert.ok(bytes instanceof WebAssembly.Module);

  configureBrowserPipelineCoreUrl("data:application/wasm;base64,AGFzbQEAAAA=");
  await assert.rejects(loadBrowserPipelineCore(), /ittm_pipeline_abi_version/);
});
