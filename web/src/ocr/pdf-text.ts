function normalizeForDedupe(text: string): string {
  return text
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[^\p{L}\p{N} ]/gu, "")
    .trim();
}

function firstContentLines(text: string, limit = 6): string {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, limit)
    .join(" ");
}

function tokenSet(text: string): Set<string> {
  const matches =
    text.toLowerCase().match(/[\p{L}][\p{L}\p{N}]{1,}|[\p{N}]{2,}/gu) ?? [];
  return new Set(matches);
}

function overlapRatio(left: Set<string>, right: Set<string>): number {
  const denominator = Math.min(left.size, right.size);
  if (denominator === 0) return 0;

  let overlap = 0;
  for (const token of left) {
    if (right.has(token)) overlap += 1;
  }
  return overlap / denominator;
}

function isUnsafePdfControlChar(char: string): boolean {
  const code = char.codePointAt(0) ?? 0;
  return (
    char === "�" ||
    (code < 32 && char !== "\n" && char !== "\r" && char !== "\t") ||
    (code >= 127 && code <= 159)
  );
}

function countUnsafePdfControlChars(text: string): number {
  return Array.from(text).filter(isUnsafePdfControlChar).length;
}

function countMojibakeMarkers(text: string): number {
  const chars = Array.from(text);
  let count = 0;

  for (let index = 0; index < chars.length; index += 1) {
    const char = chars[index];
    const nextCode = chars[index + 1]?.codePointAt(0) ?? 0;
    if (char === "Ã" || char === "Â" || char === "Ð") {
      count += 1;
    } else if (char === "Ñ" && nextCode >= 128 && nextCode <= 191) {
      count += 1;
    }
  }

  return count;
}

function countEncodingArtifacts(text: string): number {
  return countUnsafePdfControlChars(text) + countMojibakeMarkers(text);
}

function readableStats(text: string) {
  const chars = Array.from(text);
  if (chars.length === 0) {
    return {
      artifactCount: 0,
      artifactRatio: 0,
      digitRatio: 0,
      readableRatio: 1,
      letterRatio: 1,
      letterOrNumberRatio: 1,
      punctuationRatio: 0,
      repeatedDigitRunCount: 0,
      zeroRatio: 0,
    };
  }

  let artifacts = 0;
  let digits = 0;
  let letters = 0;
  let readable = 0;
  let letterOrNumber = 0;
  let zeros = 0;

  for (const char of chars) {
    if (isUnsafePdfControlChar(char)) {
      artifacts += 1;
      continue;
    }
    if (/\p{L}/u.test(char)) {
      letters += 1;
      readable += 1;
      letterOrNumber += 1;
      continue;
    }
    if (/\p{N}/u.test(char)) {
      digits += 1;
      if (char === "0") zeros += 1;
      readable += 1;
      letterOrNumber += 1;
      continue;
    }
    if (/[\s.,;:!?()[\]{}"'«»“”‘’\-–—+*/=%№#@&<>|\\/$_€£¥₽]/u.test(char)) {
      readable += 1;
    }
  }

  artifacts += countEncodingArtifacts(text) - artifacts;
  const repeatedDigitRunCount = text.match(/([0-9])\1{3,}/g)?.length ?? 0;

  return {
    artifactCount: artifacts,
    artifactRatio: artifacts / chars.length,
    digitRatio: digits / chars.length,
    readableRatio: readable / chars.length,
    letterRatio: letters / chars.length,
    letterOrNumberRatio: letterOrNumber / chars.length,
    punctuationRatio: (readable - letterOrNumber) / chars.length,
    repeatedDigitRunCount,
    zeroRatio: zeros / chars.length,
  };
}

export function isNativePdfTextTrustworthy(
  nativeText: string,
  ocrText: string,
): boolean {
  const native = nativeText.trim();
  const ocr = ocrText.trim();
  if (!native) return false;
  if (!ocr) return true;

  const stats = readableStats(native);
  if (stats.artifactCount >= 2 || stats.artifactRatio > 0.01) return false;
  if (stats.readableRatio < 0.72 || stats.letterOrNumberRatio < 0.18) {
    return false;
  }

  const nativeTokens = tokenSet(firstContentLines(native));
  const ocrTokens = tokenSet(firstContentLines(ocr));
  const enoughTextForSampleCompare =
    nativeTokens.size >= 4 && ocrTokens.size >= 4;
  const looksSuspicious =
    stats.readableRatio < 0.86 || stats.letterOrNumberRatio < 0.35;
  const looksLikePrintableGarbage =
    stats.repeatedDigitRunCount >= 2 ||
    stats.zeroRatio > 0.12 ||
    (stats.digitRatio + stats.punctuationRatio > 0.62 &&
      stats.letterRatio < 0.28);

  if (
    enoughTextForSampleCompare &&
    (looksSuspicious || looksLikePrintableGarbage) &&
    overlapRatio(nativeTokens, ocrTokens) < 0.08
  ) {
    return false;
  }

  return true;
}

export function mergeNativeAndOcrText(
  nativeText: string,
  ocrText: string,
): string {
  const parts: string[] = [];
  const native = nativeText.trim();
  const ocr = ocrText.trim();
  const trustworthyNative = isNativePdfTextTrustworthy(native, ocr);

  if (native && trustworthyNative) parts.push(native);
  if (ocr) {
    const normalizedNative = normalizeForDedupe(native);
    const normalizedOcr = normalizeForDedupe(ocr);
    const isDuplicate =
      trustworthyNative &&
      normalizedNative &&
      normalizedOcr &&
      (normalizedNative === normalizedOcr ||
        normalizedNative.includes(normalizedOcr));
    if (!isDuplicate) parts.push(ocr);
  }

  return parts.join("\n\n");
}
