import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import {
  HeadlessClientError,
  HeadlessExtractionClient,
  type HeadlessEvent,
  type HeadlessResult,
} from "./extraction-client";

const DEFAULT_TEXT_ENDPOINT = "http://localhost:3000/api/extract/text";
const DEFAULT_STREAM_ENDPOINT = "http://localhost:3000/api/tasks?sync=events";
const LEGACY_STREAM_ENDPOINT = "http://localhost:3000/api/convert/stream";
const USAGE =
  "Usage: ittm-extract <file|-> [--endpoint=URL] [--stream] [--pdf-mode=auto|raster]\n";

export interface CliIo {
  stdout(value: string): void;
  stderr(value: string): void;
  readStdin(): Promise<Uint8Array>;
}

export async function runCli(
  args: string[],
  io: CliIo,
  client = new HeadlessExtractionClient(),
  signal?: AbortSignal,
): Promise<number> {
  const fileArg = args.find((arg) => !arg.startsWith("--"));
  const endpointArg = args.find((arg) => arg.startsWith("--endpoint="));
  const pdfModeArg = args.find((arg) => arg.startsWith("--pdf-mode="));
  const stream = args.includes("--stream");
  if (!fileArg) {
    io.stderr(USAGE);
    return 2;
  }

  let bytes: Uint8Array;
  try {
    bytes =
      fileArg === "-"
        ? await io.readStdin()
        : new Uint8Array(await readFile(fileArg));
  } catch (error) {
    io.stderr(
      `Input error: ${error instanceof Error ? error.message : String(error)}\n`,
    );
    return 3;
  }

  const pdfMode = pdfModeArg?.slice("--pdf-mode=".length) || "auto";
  if (pdfMode !== "auto" && pdfMode !== "raster") {
    io.stderr(USAGE);
    return 2;
  }
  const endpoint = withPdfMode(
    endpointArg?.slice("--endpoint=".length) ||
      (stream ? DEFAULT_STREAM_ENDPOINT : DEFAULT_TEXT_ENDPOINT),
    pdfMode,
  );
  const file = new File(
    [toArrayBuffer(bytes)],
    fileArg === "-" ? "stdin.bin" : basename(fileArg),
  );
  const options = {
    signal,
    accept: stream ? "application/x-ndjson" : "text/plain",
    onEvent(event: HeadlessEvent) {
      if (stream && event.type === "page") io.stdout(event.markdown);
    },
  };

  try {
    let result: HeadlessResult;
    try {
      result = await client.extract(file, endpoint, options);
    } catch (error) {
      if (!shouldUseLegacyFallback(error, stream, Boolean(endpointArg))) {
        throw error;
      }
      result = await client.extract(
        file,
        withPdfMode(LEGACY_STREAM_ENDPOINT, pdfMode),
        options,
      );
    }

    if (!stream) io.stdout(result.markdown);
    else if (!result.markdown) io.stdout("");
    return 0;
  } catch (error) {
    if (signal?.aborted) return 130;
    io.stderr(`${error instanceof Error ? error.message : String(error)}\n`);
    return error instanceof HeadlessClientError && error.status ? 5 : 4;
  }
}

function withPdfMode(endpoint: string, pdfMode: string): string {
  if (pdfMode === "auto") return endpoint;
  const url = new URL(endpoint);
  url.searchParams.set("pdf_mode", pdfMode);
  return url.toString();
}

function shouldUseLegacyFallback(
  error: unknown,
  stream: boolean,
  hasEndpointOverride: boolean,
): boolean {
  return (
    stream &&
    !hasEndpointOverride &&
    error instanceof HeadlessClientError &&
    !error.partial &&
    (error.status === 404 || error.status === 405)
  );
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
}
