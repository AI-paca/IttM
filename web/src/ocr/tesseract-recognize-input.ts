export async function toTesseractRecognizeInput(
  input: File | Blob,
): Promise<unknown> {
  if (typeof window !== "undefined" && typeof document !== "undefined") {
    return input;
  }

  const bufferFactory = (
    globalThis as unknown as { Buffer?: { from(data: ArrayBuffer): unknown } }
  ).Buffer;
  if (bufferFactory && typeof input.arrayBuffer === "function") {
    return bufferFactory.from(await input.arrayBuffer());
  }
  return input;
}
