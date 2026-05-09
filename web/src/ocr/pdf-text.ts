function normalizeForDedupe(text: string): string {
  return text
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[^\p{L}\p{N} ]/gu, "")
    .trim();
}

export function mergeNativeAndOcrText(
  nativeText: string,
  ocrText: string,
): string {
  const parts: string[] = [];
  const native = nativeText.trim();
  const ocr = ocrText.trim();

  if (native) parts.push(native);
  if (ocr) {
    const normalizedNative = normalizeForDedupe(native);
    const normalizedOcr = normalizeForDedupe(ocr);
    const isDuplicate =
      normalizedNative &&
      normalizedOcr &&
      (normalizedNative === normalizedOcr ||
        normalizedNative.includes(normalizedOcr));
    if (!isDuplicate) parts.push(ocr);
  }

  return parts.join("\n\n");
}
