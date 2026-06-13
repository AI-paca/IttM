import assert from "node:assert/strict";
import test from "node:test";
import { readableStreamToBase64 } from "./base64-stream";

test("readableStreamToBase64 preserves byte groups across stream chunks", async () => {
  const chunks = [
    new Uint8Array([0, 1]),
    new Uint8Array([2, 3, 4, 5, 6]),
    new Uint8Array([7, 8, 9]),
  ];
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = chunks.shift();
      if (next) controller.enqueue(next);
      else controller.close();
    },
  });

  const encoded = await readableStreamToBase64(stream);

  assert.equal(
    encoded,
    Buffer.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]).toString("base64"),
  );
});
