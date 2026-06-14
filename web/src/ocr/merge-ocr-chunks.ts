function normalizeLine(value: string): string {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

export function mergeOcrTextChunks(chunks: readonly string[]): string {
  const output: string[] = [];

  for (const chunk of chunks) {
    const incoming = chunk.split(/\r?\n/);
    const normalizedOutput = output.map(normalizeLine);
    const normalizedIncoming = incoming.map(normalizeLine);
    const maxOverlap = Math.min(
      20,
      normalizedOutput.length,
      normalizedIncoming.length,
    );
    let overlap = 0;

    for (let size = maxOverlap; size > 0; size -= 1) {
      const left = normalizedOutput.slice(-size);
      const right = normalizedIncoming.slice(0, size);
      if (
        left.every((line, index) => line.length > 0 && line === right[index])
      ) {
        overlap = size;
        break;
      }
    }
    output.push(...incoming.slice(overlap));
  }

  return output.join("\n").trim();
}
