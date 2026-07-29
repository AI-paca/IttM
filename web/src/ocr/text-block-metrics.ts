export function normalizeOcrText(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

export function ocrCompactCharCount(value: string): number {
  return value.match(/[\p{L}\p{N}_]/gu)?.length ?? 0;
}

export function normalizedOcrTokens(value: string): string[] {
  return normalizeOcrText(value).match(/[\p{L}\p{N}_]+/gu) ?? [];
}

export function fallbackEvidenceTokens(value: string): string[] {
  const tokens = normalizedOcrTokens(value);
  const evidence = tokens.filter(
    (token) => token.length >= 4 || /\p{N}/u.test(token),
  );
  return evidence.length ? evidence : tokens;
}

export function sharedOcrTokenCount(
  left: readonly string[],
  right: readonly string[],
): number {
  const available = new Map<string, number>();
  for (const token of left) {
    available.set(token, (available.get(token) ?? 0) + 1);
  }

  let shared = 0;
  for (const token of right) {
    const count = available.get(token) ?? 0;
    if (count <= 0) continue;
    shared += 1;
    available.set(token, count - 1);
  }
  return shared;
}
