import assert from "node:assert/strict";
import test from "node:test";

import { BrowserPipelineCore } from "./pipeline-core";
import {
  runTextPipeline,
  trustedMarkdownArtifact,
} from "./pipeline-orchestrator";

function coreWithMask(mask: number) {
  return new BrowserPipelineCore({
    ittm_pipeline_abi_version: () => 3,
    ittm_pipeline_recipe_mask: () => mask,
    ittm_sparse_add_signal: () => 0,
    ittm_is_isolated_heading: () => 0,
    ittm_span_evidence_score: () => 0,
    ittm_should_replace_primary: () => 0,
    ittm_should_drop_text_block: () => 0,
  });
}

test("trusted Markdown cannot invoke local OCR repair handlers", async () => {
  const fail = () => {
    throw new Error("local handler must be bypassed");
  };
  const result = await runTextPipeline(
    trustedMarkdownArtifact("trusted | markdown"),
    {
      select_language_candidate: fail,
      lexical_correction: fail,
      group_structures: fail,
      render_markdown: fail,
    },
    coreWithMask(1 << 3),
  );

  assert.equal(result.markdown, "trusted | markdown");
  assert.deepEqual(result.meta?.pipeline, {
    capabilities: {
      trustedText: true,
      providesLayout: true,
      providesMarkdown: true,
      needsLanguageRetry: false,
    },
    stages: ["recognize_segments"],
  });
});

test("local text handlers execute in shared recipe order", async () => {
  const result = await runTextPipeline(
    { text: "ocr", capabilities: {} },
    {
      lexical_correction: (text) => `${text}:lexical`,
      group_structures: (text) => `${text}:group`,
      render_markdown: (text) => `${text}:render`,
    },
    coreWithMask((1 << 5) | (1 << 6) | (1 << 7)),
  );
  assert.equal(result.markdown, "ocr:lexical:group:render");
});
