export function parseTextReviewerDecision(value: string): boolean | null {
  const normalized = value.trim().toUpperCase();
  const label = normalized.match(/^(TEXT|NOISE)\b/)?.[1];
  if (
    label === "NOISE" ||
    /\bRANDOM GLYPHS?\b/.test(normalized) ||
    /\bEMPTY CELL\b/.test(normalized)
  ) {
    return false;
  }
  if (
    label === "TEXT" ||
    /\bMEANINGFUL (?:TEXT|WORD|NUMBER|IDENTIFIER|FORMULA|SYMBOL)\b/.test(
      normalized,
    )
  ) {
    return true;
  }
  return null;
}
