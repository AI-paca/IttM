import test from "node:test";
import assert from "node:assert/strict";
import {
  extractVisibleMessages,
  messagesToMarkdown,
  splitUnicode,
  type ContentNode,
} from "./content-extractor";

test("content extraction is iterative across fifty thousand nested nodes", () => {
  let root: ContentNode = {
    role: "assistant",
    text: "deep answer",
  };
  for (let index = 0; index < 50_000; index += 1) {
    root = { children: [root] };
  }

  assert.deepEqual(extractVisibleMessages(root), [
    { role: "assistant", text: "deep answer" },
  ]);
});

test("hidden DOM-like branches never reach extracted chat content", () => {
  const root: ContentNode = {
    children: [
      { role: "user", text: "visible prompt" },
      {
        hidden: true,
        children: [
          { role: "system", text: "hidden token=secret" },
          { role: "assistant", text: "invisible menu" },
        ],
      },
      { role: "assistant", text: "visible answer" },
    ],
  };

  assert.deepEqual(extractVisibleMessages(root), [
    { role: "user", text: "visible prompt" },
    { role: "assistant", text: "visible answer" },
  ]);
});

test("payload-looking text remains inert text instead of executable HTML", () => {
  const payload = '<img src="x" onerror="globalThis.pwned=true">';
  const messages = extractVisibleMessages({
    role: "assistant",
    text: payload,
  });

  assert.equal(messages[0].text, payload);
  assert.match(messagesToMarkdown(messages), /onerror=/);
  assert.equal((globalThis as { pwned?: boolean }).pwned, undefined);
});

test("Unicode chunks never split surrogate pairs or astral symbols", () => {
  const value = "A𝌆🙂B";
  const chunks = splitUnicode(value, 2);

  assert.deepEqual(chunks, ["A𝌆", "🙂B"]);
  assert.equal(chunks.join(""), value);
});

test("large code blocks are bounded explicitly instead of overflowing stack", () => {
  const text = "x".repeat(3_000_000);

  assert.equal(
    extractVisibleMessages(
      { role: "assistant", text },
      { maxCharacters: 3_000_000 },
    )[0].text.length,
    3_000_000,
  );
  assert.throws(
    () =>
      extractVisibleMessages(
        { role: "assistant", text },
        { maxCharacters: 2_000_000 },
      ),
    /exceeds/,
  );
});
