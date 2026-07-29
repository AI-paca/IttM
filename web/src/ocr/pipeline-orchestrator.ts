import type { OcrResult } from "./types";
import {
  type BrowserPipelineCore,
  loadBrowserPipelineCore,
  type PipelineCapabilities,
  type PipelineStage,
} from "./pipeline-core";

export interface TextRecognitionArtifact {
  text: string;
  capabilities: PipelineCapabilities;
}

export type TextStageHandler = (text: string) => string | Promise<string>;
export type TextStageHandlers = Partial<
  Record<
    | "select_language_candidate"
    | "lexical_correction"
    | "group_structures"
    | "render_markdown",
    TextStageHandler
  >
>;

export async function runTextPipeline(
  artifact: TextRecognitionArtifact,
  handlers: TextStageHandlers = {},
  core?: BrowserPipelineCore,
): Promise<OcrResult> {
  const pipelineCore = core ?? (await loadBrowserPipelineCore());
  const recipe = pipelineCore.recipe(artifact.capabilities);
  let text = artifact.text;
  for (const stage of recipe) {
    const handler = handlers[stage as keyof TextStageHandlers];
    if (handler) text = await handler(text);
  }
  return {
    markdown: text,
    meta: {
      pipeline: {
        capabilities: artifact.capabilities,
        stages: [...recipe] satisfies PipelineStage[],
      },
    },
  };
}

export function trustedMarkdownArtifact(
  markdown: string,
): TextRecognitionArtifact {
  return {
    text: markdown,
    capabilities: {
      trustedText: true,
      providesLayout: true,
      providesMarkdown: true,
      needsLanguageRetry: false,
    },
  };
}
