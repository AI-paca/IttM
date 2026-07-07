/// <reference lib="webworker" />

import { env, pipeline } from "@huggingface/transformers";
import { parseTextReviewerDecision } from "./text-reviewer-decision";
import type {
  TextReviewerRequest,
  TextReviewerResponse,
} from "./text-reviewer-protocol";

const MODEL_ID = "HuggingFaceTB/SmolLM2-135M-Instruct";
const baseUrl = import.meta.env.BASE_URL || "/";
const normalizedBase = baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;

env.allowLocalModels = true;
env.allowRemoteModels = false;
env.useBrowserCache = true;
env.localModelPath = `${normalizedBase}vendor/models/`;

const onnxBackend = env.backends.onnx as {
  wasm?: {
    numThreads?: number;
    wasmPaths?: {
      mjs: string;
      wasm: string;
    };
  };
};
onnxBackend.wasm ??= {};
onnxBackend.wasm.numThreads = 1;
onnxBackend.wasm.wasmPaths = {
  mjs: `${normalizedBase}vendor/transformers/wasm/ort-wasm-simd-threaded.mjs`,
  wasm: `${normalizedBase}vendor/transformers/wasm/ort-wasm-simd-threaded.wasm`,
};

type TextGenerator = (
  messages: Array<{ role: string; content: string }>,
  options: {
    max_new_tokens: number;
    do_sample: boolean;
    return_full_text: boolean;
  },
) => Promise<unknown>;
let generatorPromise: Promise<TextGenerator> | null = null;

function generator(): Promise<TextGenerator> {
  generatorPromise ??= pipeline("text-generation", MODEL_ID, {
    device: "wasm",
    dtype: "q8",
    local_files_only: true,
  }) as Promise<unknown> as Promise<TextGenerator>;
  return generatorPromise;
}

function candidatePrompt(request: TextReviewerRequest): string {
  const { text, left = "", top = "", confidence } = request.candidate;
  const confidenceLine =
    typeof confidence === "number"
      ? `OCR confidence: ${confidence.toFixed(1)}`
      : "OCR confidence: unknown";
  return [
    "Classify one OCR cell candidate.",
    "Answer exactly TEXT or NOISE.",
    "TEXT includes meaningful words in any language, personal names, identifiers, numbers, and mathematical formulas.",
    "NOISE means random glyphs produced from an otherwise empty cell.",
    "Never rewrite or spell-correct the candidate.",
    "Examples:",
    "Candidate: Уварова; confidence: 71; left: Зав. кафедрой => TEXT",
    "Candidate: Παπαδόπουλος; confidence: 68 => TEXT",
    "Candidate: 张伟; confidence: 74 => TEXT",
    "Candidate: БЕ; confidence: 8; left: 4; upper: <empty> => NOISE",
    "Candidate: |_'; confidence: 5; left: <empty>; upper: <empty> => NOISE",
    `Left cell: ${left || "<empty>"}`,
    `Upper cell: ${top || "<empty>"}`,
    confidenceLine,
    `Candidate: ${text}`,
  ].join("\n");
}

function generatedText(output: unknown): string {
  if (!Array.isArray(output) || output.length === 0) return "";
  const first = output[0] as {
    generated_text?: string | Array<{ content?: string }>;
  };
  if (typeof first.generated_text === "string") {
    return first.generated_text;
  }
  if (Array.isArray(first.generated_text)) {
    return first.generated_text.at(-1)?.content || "";
  }
  return "";
}

self.onmessage = async (event: MessageEvent<TextReviewerRequest>) => {
  const request = event.data;
  if (request.type !== "review") return;

  try {
    const generate = await generator();
    const output = await generate(
      [
        {
          role: "system",
          content:
            "You are a strict OCR text-versus-noise gate. Follow the requested output format.",
        },
        {
          role: "user",
          content: candidatePrompt(request),
        },
      ],
      {
        max_new_tokens: 8,
        do_sample: false,
        return_full_text: false,
      },
    );
    const response: TextReviewerResponse = {
      id: request.id,
      type: "result",
      isText: parseTextReviewerDecision(generatedText(output)) ?? true,
    };
    self.postMessage(response);
  } catch (error) {
    const response: TextReviewerResponse = {
      id: request.id,
      type: "error",
      error: error instanceof Error ? error.message : String(error),
    };
    self.postMessage(response);
  }
};

export {};
