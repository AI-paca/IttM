export type HeadlessEvent =
  | { type: "accepted"; taskId: string }
  | {
      type: "progress";
      stage: string;
      page?: number;
      percent?: number;
      totalPages?: number;
    }
  | { type: "page"; page: number; markdown: string; totalPages?: number }
  | { type: "warning"; code: string; message: string }
  | {
      type: "error";
      detail?: string;
      message?: string;
      partial?: boolean;
    }
  | { type: "complete"; meta: Record<string, unknown> };

export class HeadlessClientError extends Error {
  constructor(
    message: string,
    readonly partial: boolean,
    readonly status?: number,
  ) {
    super(message);
    this.name = "HeadlessClientError";
  }
}

export interface HeadlessResult {
  markdown: string;
  meta: Record<string, unknown>;
}

export class HeadlessExtractionClient {
  constructor(private readonly fetchImpl: typeof fetch = fetch) {}

  async extract(
    file: File,
    url: string,
    {
      signal,
      onEvent,
      accept,
    }: {
      signal?: AbortSignal;
      onEvent?: (event: HeadlessEvent) => void;
      accept?: string;
    } = {},
  ): Promise<HeadlessResult> {
    const form = new FormData();
    form.append("file", file);
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method: "POST",
        headers: accept ? { accept } : undefined,
        body: form,
        signal,
      });
    } catch (error) {
      if (signal?.aborted) {
        throw new DOMException("Extraction aborted.", "AbortError");
      }
      throw new HeadlessClientError(
        error instanceof Error ? error.message : String(error),
        false,
      );
    }

    const contentType = response.headers.get("content-type") ?? "";
    if (!response.ok) {
      const responseMessage =
        contentType.includes("text/plain") ||
        contentType.includes("text/markdown")
          ? (await response.text()).trim()
          : "";
      throw new HeadlessClientError(
        responseMessage || `OCR endpoint returned HTTP ${response.status}.`,
        false,
        response.status,
      );
    }
    if (
      response.headers.get("content-type")?.includes("application/x-ndjson")
    ) {
      return await this.readStream(response, onEvent);
    }

    if (
      contentType.includes("text/plain") ||
      contentType.includes("text/markdown")
    ) {
      return { markdown: await response.text(), meta: {} };
    }

    const payload = (await response.json()) as {
      markdown?: unknown;
      meta?: unknown;
      detail?: unknown;
      error?: unknown;
    };
    if (typeof payload.markdown !== "string") {
      throw new HeadlessClientError(
        String(payload.detail || payload.error || "Invalid OCR JSON response."),
        false,
        response.status,
      );
    }
    return {
      markdown: payload.markdown,
      meta:
        payload.meta && typeof payload.meta === "object"
          ? (payload.meta as Record<string, unknown>)
          : {},
    };
  }

  private async readStream(
    response: Response,
    onEvent?: (event: HeadlessEvent) => void,
  ): Promise<HeadlessResult> {
    if (!response.body) {
      throw new HeadlessClientError("OCR stream has no body.", false);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const pages: string[] = [];
    let meta: Record<string, unknown> | null = null;
    let buffered = "";

    const consume = (line: string) => {
      if (!line.trim()) return;
      let event: HeadlessEvent;
      try {
        event = JSON.parse(line) as HeadlessEvent;
      } catch {
        throw new HeadlessClientError(
          "OCR stream contains malformed NDJSON.",
          pages.length > 0,
        );
      }
      onEvent?.(event);
      if (event.type === "page") {
        pages.push(event.markdown);
      } else if (event.type === "error") {
        throw new HeadlessClientError(
          event.message || event.detail || "OCR worker reported an error.",
          Boolean(event.partial) || pages.length > 0,
        );
      } else if (event.type === "complete") {
        meta = event.meta;
      } else if (
        event.type !== "accepted" &&
        event.type !== "progress" &&
        event.type !== "warning"
      ) {
        throw new HeadlessClientError(
          "OCR stream contains an unknown event.",
          pages.length > 0,
        );
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      const lines = buffered.split("\n");
      buffered = lines.pop() || "";
      lines.forEach(consume);
    }
    buffered += decoder.decode();
    if (buffered.trim()) consume(buffered);
    if (!meta) {
      throw new HeadlessClientError(
        "OCR stream ended before completion.",
        pages.length > 0,
      );
    }
    return { markdown: pages.join("\n\n---\n\n"), meta };
  }
}
