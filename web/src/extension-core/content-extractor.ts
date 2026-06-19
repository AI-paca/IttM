export interface ContentNode {
  text?: string;
  hidden?: boolean;
  role?: "user" | "assistant" | "system";
  children?: readonly ContentNode[];
}

export interface ExtractedMessage {
  role: "user" | "assistant" | "system";
  text: string;
}

export function splitUnicode(value: string, maxCodePoints: number): string[] {
  if (maxCodePoints <= 0) {
    throw new RangeError("maxCodePoints must be positive.");
  }

  const chunks: string[] = [];
  let current: string[] = [];
  for (const codePoint of value) {
    current.push(codePoint);
    if (current.length === maxCodePoints) {
      chunks.push(current.join(""));
      current = [];
    }
  }
  if (current.length > 0) chunks.push(current.join(""));
  return chunks;
}

export function extractVisibleMessages(
  root: ContentNode,
  {
    maxNodes = 100_000,
    maxCharacters = 5_000_000,
  }: { maxNodes?: number; maxCharacters?: number } = {},
): ExtractedMessage[] {
  const stack: ContentNode[] = [root];
  const messages: ExtractedMessage[] = [];
  let visited = 0;
  let characters = 0;

  while (stack.length > 0) {
    const node = stack.pop()!;
    visited += 1;
    if (visited > maxNodes) {
      throw new RangeError(`Content tree exceeds ${maxNodes} nodes.`);
    }
    if (node.hidden) continue;

    if (node.role && node.text) {
      characters += [...node.text].length;
      if (characters > maxCharacters) {
        throw new RangeError(
          `Visible content exceeds ${maxCharacters} characters.`,
        );
      }
      messages.push({ role: node.role, text: node.text });
    }

    const children = node.children || [];
    for (let index = children.length - 1; index >= 0; index -= 1) {
      stack.push(children[index]);
    }
  }

  return messages;
}

export function messagesToMarkdown(messages: readonly ExtractedMessage[]) {
  return messages
    .map(({ role, text }) => `**${role}:**\n\n${text}`)
    .join("\n\n---\n\n");
}
