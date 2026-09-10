export interface MathLanguageScores {
  equ: number;
  ell: number;
}

const MATH_SYMBOLS = /[+\-*/=<>≤≥≈≠±×÷√∫∑∏∂∇∞∝∈∉∀∃⊕⊖⊗⊘⊙]/gu;
const MATH_OPERATORS = /[+\-*/=<>≤≥≈≠]/gu;
const SUPERSCRIPTS = /[⁰¹²³⁴⁵⁶⁷⁸⁹]/gu;
const FRACTIONS = /(?<![\p{L}\p{N}_])\d+\s*\/\s*\d+(?![\p{L}\p{N}_])/gu;
const VARIABLES = /(?<![\p{L}\p{N}_])[A-Za-z\u0370-\u03ff](?![\p{L}\p{N}_])/gu;
const NUMBERS = /(?<![\p{L}\p{N}_])\d+(?:[.,]\d+)?(?![\p{L}\p{N}_])/gu;
const DATE_LIKE =
  /^\s*\d{1,4}([./-])\d{1,2}\1\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$/u;
const GREEK_LETTERS = /[\u0370-\u03ff]/gu;

const WORD_UNIT = String.raw`(?:mol\/L|GHz|MHz|kHz|Hz|MPa|kPa|Pa|cm|mm|nm|um|µm|km|ft|in|lb|oz|mg|kg|ms|µs|us|ns|min|kA|mA|mV|kV|kW|MW|GW|bar|psi|mol|rad|sr|m²|m3|[kMGT]?m|g|t|s|h|A|V|W|J|N|%|K|l|L)`;
const SYMBOL_UNIT = String.raw`(?:°C|°F|°|Ω|%)`;
const SPACED_UNIT = String.raw`(?:${WORD_UNIT}\b|${SYMBOL_UNIT})`;
const ATTACHED_UNIT = String.raw`(?:cm|mm|nm|um|µm|km|ft|in|lb|oz|mg|kg|ms|µs|us|ns|min|kA|mA|mV|kV|kW|MW|GW|Pa|kPa|MPa|bar|psi|Hz|kHz|MHz|GHz|°C|°F|°|mol|Ω|%|K|rad|sr|m²|m3|mol\/L)`;
const PER_UNIT = String.raw`(?:[kMGT]?(?:m|s|A|V|W|Hz)|mol|%|rad|sr|m²|m3)`;
const MEASUREMENT_UNIT = new RegExp(
  String.raw`(?<![\p{L}\p{N}_.])\d+(?:[.,]\d+)?(?:\s+${SPACED_UNIT}|${ATTACHED_UNIT})(?:\s*\/\s*${PER_UNIT})?(?![\p{L}\p{N}_])`,
  "giu",
);

function countMatches(text: string, pattern: RegExp): number {
  return text.match(pattern)?.length ?? 0;
}

export function scoreMathLanguage(text: string): MathLanguageScores {
  const normalized = text.trim();
  if (!normalized) return { equ: 0, ell: 0 };

  const mathSymbols = countMatches(normalized, MATH_SYMBOLS);
  const operators = countMatches(normalized, MATH_OPERATORS);
  const superscripts = countMatches(normalized, SUPERSCRIPTS);
  const fractions = countMatches(normalized, FRACTIONS);
  const variables = countMatches(normalized, VARIABLES);
  const digits = countMatches(normalized, NUMBERS);
  const greekLetters = countMatches(normalized, GREEK_LETTERS);
  const unitCount = countMatches(normalized, MEASUREMENT_UNIT);
  const isDateLike = DATE_LIKE.test(normalized);
  const hasFormulaSignature =
    !isDateLike && operators > 0 && (variables > 0 || digits > 0);

  let equ = 0;
  let ell = 0;

  if (hasFormulaSignature) {
    equ += 2.4;
    equ += Math.min(2.5, operators * 0.55);
  }
  if (mathSymbols) {
    equ += Math.min(1.8, mathSymbols * 0.25);
    if (greekLetters) ell += 0.7;
  }
  if (unitCount) equ += Math.min(2.2, unitCount);
  if (fractions) equ += Math.min(1.8, fractions * 1.2);
  if (superscripts) equ += Math.min(1.2, superscripts * 0.4);

  if (greekLetters) {
    ell += Math.min(2.4, greekLetters * 0.35);
    if (hasFormulaSignature || unitCount) {
      ell += 0.8;
      equ += 0.4;
    }
  }

  if (isDateLike && !hasFormulaSignature) equ *= 0.15;

  return { equ, ell };
}
