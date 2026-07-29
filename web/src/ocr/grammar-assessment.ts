import initOcrCore, {
  assess_grammar_json,
} from "../wasm/ocr-core/ittm_ocr_core.js";

export interface BrowserGrammarAssessment {
  percent: number;
  exact: boolean;
  reasons: string[];
}

let initPromise: Promise<unknown> | null = null;

export async function assessBrowserGrammar(
  text: string,
  languages: string,
  wordConfidences: readonly number[],
): Promise<BrowserGrammarAssessment> {
  initPromise ??= initOcrCore();
  await initPromise;
  return JSON.parse(
    assess_grammar_json(
      text,
      languages,
      Float64Array.from(wordConfidences),
    ),
  ) as BrowserGrammarAssessment;
}

