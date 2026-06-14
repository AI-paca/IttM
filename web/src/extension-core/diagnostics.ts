const ZERO_WIDTH = /[\u200B-\u200D\u2060\uFEFF]/g;

export function sanitizeDiagnostic(input: unknown, maxLength = 2000): string {
  const raw =
    input instanceof Error ? `${input.name}: ${input.message}` : String(input);
  const normalized = raw.replace(ZERO_WIDTH, "");
  return normalized
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]")
    .replace(
      /\b(api[_-]?key|token|authorization)\s*[:=]\s*[^\s,;]+/gi,
      "$1=[REDACTED]",
    )
    .replace(/\b(?:\d[ -]?){13,19}\b/g, "[REDACTED-PAN]")
    .slice(0, maxLength);
}
