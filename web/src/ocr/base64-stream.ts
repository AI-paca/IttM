const BASE64_INPUT_CHUNK = 3 * 8192;

function encodeAlignedBytes(bytes: Uint8Array): string {
  let result = "";
  for (let offset = 0; offset < bytes.length; offset += BASE64_INPUT_CHUNK) {
    const chunk = bytes.subarray(
      offset,
      Math.min(bytes.length, offset + BASE64_INPUT_CHUNK),
    );
    let binary = "";
    for (let index = 0; index < chunk.length; index += 1) {
      binary += String.fromCharCode(chunk[index]);
    }
    result += btoa(binary);
  }
  return result;
}

export async function readableStreamToBase64(
  stream: ReadableStream<Uint8Array>,
): Promise<string> {
  const reader = stream.getReader();
  let carry = new Uint8Array(0);
  let result = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value?.length) continue;

      const combined = new Uint8Array(carry.length + value.length);
      combined.set(carry);
      combined.set(value, carry.length);
      const alignedLength = combined.length - (combined.length % 3);

      if (alignedLength > 0) {
        result += encodeAlignedBytes(combined.subarray(0, alignedLength));
      }
      carry = combined.slice(alignedLength);
    }

    if (carry.length) result += encodeAlignedBytes(carry);
    return result;
  } finally {
    reader.releaseLock();
  }
}
