import type { Env } from "../domain/types";
import {
  error_response,
  json_response,
  method_not_allowed,
} from "../core/http";
import {
  TaskCapacityError,
  TaskExecutionError,
  TaskService,
} from "./task-service";
import type {
  ExtractionEngine,
  ExtractionError,
  ExtractionEvent,
  ExtractionMeta,
  ExtractionRequest,
  TaskRecord,
  TaskState,
  WorkerContext,
  WorkerExecutor,
  WorkerEventInput,
} from "./types";

type SyncMode = "async" | "events" | "text" | "markdown" | "json";

const LOCAL_TASK_ENGINES = new Set<ExtractionEngine>([
  "auto",
  "tesseract",
  "easyocr",
]);
const TERMINAL_EVENT_TYPES = new Set(["complete", "error"]);
const VALID_TASK_STATES = new Set<TaskState>([
  "queued",
  "running",
  "cancelling",
  "completed",
  "failed",
  "cancelled",
]);
const DEFAULT_TASK_LIST_LIMIT = 50;
const MAX_TASK_LIST_LIMIT = 100;
const DEFAULT_EVENTS_DISCONNECT_GRACE_MS = 250;
const taskServices = new Map<string, TaskService>();
const activeSchedulers = new WeakSet<TaskService>();
type EventStreamWatchState = {
  activeWatchers: number;
  timer?: ReturnType<typeof setTimeout>;
};
const eventStreamWatchStates = new WeakMap<
  TaskService,
  Map<string, EventStreamWatchState>
>();
const eventStreamWatchStateMaps = new Set<Map<string, EventStreamWatchState>>();

export async function handleTaskApi(
  request: Request,
  env: Env,
): Promise<Response | null> {
  const url = new URL(request.url);
  const match = matchTaskRoute(url.pathname);
  if (!match) return null;

  const service = getTaskService(env);

  if (match.kind === "extractText") {
    if (request.method !== "POST") return method_not_allowed();
    return await createTaskResponse(request, env, service, "text");
  }

  if (match.kind === "collection") {
    if (request.method === "GET") return listTasksResponse(url, service);
    if (request.method !== "POST") return method_not_allowed();
    return await createTaskResponse(request, env, service);
  }

  const record = service.get(match.id);
  if (!record) return error_response("Task not found.", 404);

  if (match.kind === "item") {
    if (request.method !== "GET") return method_not_allowed();
    const since = numberParam(url.searchParams.get("since"));
    return json_response(serializeTaskRecord(record, since));
  }

  if (match.kind === "events") {
    if (request.method !== "GET") return method_not_allowed();
    return streamTaskEvents(
      request,
      service,
      record.id,
      resolveSince(request, url),
      eventsDisconnectGraceMs(env),
    );
  }

  if (request.method !== "POST") return method_not_allowed();
  const cancelled = service.cancel(record.id);
  return json_response(serializeTaskRecord(cancelled ?? record));
}

export function resetTaskApiForTests(): void {
  clearEventStreamWatchStates();
  taskServices.clear();
}

function getTaskService(env: Env): TaskService {
  const key = env.OCR_URL;
  const existing = taskServices.get(key);
  if (existing) return existing;

  const service = new TaskService(new OcrStreamTaskExecutor(env), {
    maxQueued: 32,
    maxWorkers: 1,
  });
  taskServices.set(key, service);
  return service;
}

function schedule(service: TaskService): void {
  if (activeSchedulers.has(service)) return;
  activeSchedulers.add(service);
  void (async () => {
    try {
      while ((await service.runNext()) !== null) {
        // Keep draining the local in-memory queue while capacity is available.
      }
    } finally {
      activeSchedulers.delete(service);
    }
  })();
}

async function createTaskResponse(
  request: Request,
  env: Env,
  service: TaskService,
  syncOverride?: SyncMode,
): Promise<Response> {
  let extractionRequest: ExtractionRequest;
  try {
    extractionRequest = await toExtractionRequest(request);
  } catch (error) {
    return error_response(
      error instanceof Error ? error.message : String(error),
      400,
    );
  }

  let record: TaskRecord;
  try {
    record = service.create(extractionRequest);
  } catch (error) {
    if (error instanceof TaskCapacityError) {
      return error_response("Task queue capacity exceeded.", 503);
    }
    throw error;
  }

  schedule(service);
  const sync = syncOverride ?? resolveSyncMode(request);

  if (sync === "async") {
    return json_response(
      {
        taskId: record.id,
        event: record.events[0],
        task: serializeTaskRecord(record),
      },
      202,
      { Location: `/api/tasks/${record.id}` },
    );
  }

  if (sync === "events") {
    return streamTaskEvents(
      request,
      service,
      record.id,
      0,
      eventsDisconnectGraceMs(env),
    );
  }

  return await waitForSyncResult(request, env, service, record.id, sync);
}

async function waitForSyncResult(
  request: Request,
  _env: Env,
  service: TaskService,
  id: string,
  sync: Exclude<SyncMode, "async" | "events">,
): Promise<Response> {
  const abort = () => service.cancel(id);
  let abortListenerAttached = false;
  if (request.signal.aborted) {
    abort();
  } else {
    request.signal.addEventListener("abort", abort, { once: true });
    abortListenerAttached = true;
  }
  try {
    if (!request.signal.aborted) {
      const current = service.get(id);
      if (!current || !isTerminalRecord(current)) {
        const since = current?.events.length ?? 0;
        for await (const event of service.watch(id, {
          since,
          signal: request.signal,
        })) {
          if (TERMINAL_EVENT_TYPES.has(event.type)) break;
        }
      }
    }
  } finally {
    if (abortListenerAttached) {
      request.signal.removeEventListener("abort", abort);
    }
  }

  const record = service.get(id);
  if (!record) return error_response("Task not found.", 404);
  if (
    request.signal.aborted ||
    record.state === "cancelling" ||
    record.state === "cancelled"
  ) {
    return new Response(null, { status: 499 });
  }

  if (record.state !== "completed" || !record.result) {
    const error = record.error ?? {
      code: "WORKER_FAILED",
      message: "Task did not complete.",
      retryable: false,
      partial: false,
      httpStatus: 502,
    };
    if (sync === "json")
      return json_response({ error }, error.httpStatus ?? 502);
    return textResponse(
      `error: ${error.code}: ${error.message}\n`,
      error.httpStatus ?? statusForError(error),
      sync === "markdown"
        ? "text/markdown; charset=utf-8"
        : "text/plain; charset=utf-8",
    );
  }

  if (sync === "json") return json_response(record.result);
  if (sync === "markdown") {
    return textResponse(
      record.result.markdown,
      200,
      "text/markdown; charset=utf-8",
      {
        "X-Markdown-Meta": base64Json(record.result.meta),
        "X-Ocr-Warnings": String(record.result.warnings.length),
      },
    );
  }
  return textResponse(
    record.result.markdown,
    200,
    "text/plain; charset=utf-8",
    {
      "X-Ocr-Warnings": String(record.result.warnings.length),
    },
  );
}

function isTerminalRecord(record: TaskRecord): boolean {
  return (
    record.state === "completed" ||
    record.state === "failed" ||
    record.state === "cancelled"
  );
}

async function toExtractionRequest(
  request: Request,
): Promise<ExtractionRequest> {
  const url = new URL(request.url);
  const contentType = request.headers.get("content-type") ?? "";
  const engine = readEngine(request, url);
  const profile =
    url.searchParams.get("profile") ??
    url.searchParams.get("pipeline_profile") ??
    request.headers.get("x-ocr-profile") ??
    undefined;
  const pdfMode = readPdfMode(request, url);

  if (contentType.includes("application/json")) {
    const payload = (await request.json()) as Partial<ExtractionRequest>;
    return {
      filename:
        typeof payload.filename === "string" ? payload.filename : "upload",
      engine: readEngine(request, url, payload.engine),
      profile: typeof payload.profile === "string" ? payload.profile : profile,
      pdfMode:
        payload.pdfMode === undefined
          ? pdfMode
          : normalizePdfMode(payload.pdfMode),
      source: payload.source,
      budgets: payload.budgets,
      privacy: payload.privacy,
      contentType: payload.contentType,
      language: payload.language,
      pageHints: payload.pageHints,
    };
  }

  if (contentType.includes("multipart/form-data")) {
    const form = await request.formData();
    const file = form.get("file");
    if (!(file instanceof File))
      throw new Error("Multipart task upload must include a file field.");
    return {
      filename: file.name || url.searchParams.get("filename") || "upload",
      engine,
      profile,
      pdfMode,
      source: { kind: "file", file },
      contentType: file.type || undefined,
    };
  }

  const body = await request.arrayBuffer();
  if (!body.byteLength) throw new Error("Task upload is empty.");
  const declaredContentType =
    contentType.split(";")[0] || "application/octet-stream";
  const inferredContentType = inferBinaryContentType(new Uint8Array(body));
  const binaryContentType =
    isGenericBinaryContentType(declaredContentType) && inferredContentType
      ? inferredContentType
      : declaredContentType;
  const filename =
    url.searchParams.get("filename") || defaultFilename(binaryContentType);
  const file = new File([body], filename, { type: binaryContentType });
  return {
    filename,
    engine,
    profile,
    pdfMode,
    source: { kind: "file", file },
    contentType: binaryContentType,
  };
}

function readPdfMode(request: Request, url: URL): ExtractionRequest["pdfMode"] {
  const raw =
    url.searchParams.get("pdf_mode") ??
    request.headers.get("x-pdf-mode") ??
    "auto";
  return normalizePdfMode(raw);
}

function normalizePdfMode(value: unknown): "auto" | "raster" {
  if (value !== "auto" && value !== "raster") {
    throw new Error("Unsupported PDF mode. Expected auto or raster.");
  }
  return value;
}

function readEngine(
  request: Request,
  url: URL,
  fallback?: unknown,
): ExtractionEngine {
  const raw =
    url.searchParams.get("engine") ??
    url.searchParams.get("engine_type") ??
    request.headers.get("x-ocr-engine") ??
    (typeof fallback === "string" ? fallback : undefined) ??
    "auto";
  if (!LOCAL_TASK_ENGINES.has(raw as ExtractionEngine)) {
    throw new Error("Unsupported OCR engine for local task API.");
  }
  return raw as ExtractionEngine;
}

function isGenericBinaryContentType(contentType: string): boolean {
  return (
    contentType === "application/octet-stream" ||
    contentType === "application/x-www-form-urlencoded"
  );
}

function inferBinaryContentType(bytes: Uint8Array): string | undefined {
  if (
    bytes.length >= 5 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46 &&
    bytes[4] === 0x2d
  ) {
    return "application/pdf";
  }
  if (
    bytes.length >= 8 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return "image/png";
  }
  if (
    bytes.length >= 3 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[2] === 0xff
  ) {
    return "image/jpeg";
  }
  if (
    bytes.length >= 12 &&
    bytes[0] === 0x52 &&
    bytes[1] === 0x49 &&
    bytes[2] === 0x46 &&
    bytes[3] === 0x46 &&
    bytes[8] === 0x57 &&
    bytes[9] === 0x45 &&
    bytes[10] === 0x42 &&
    bytes[11] === 0x50
  ) {
    return "image/webp";
  }
  return undefined;
}

function defaultFilename(contentType: string): string {
  if (contentType === "image/png") return "screenshot.png";
  if (contentType === "image/jpeg") return "screenshot.jpg";
  if (contentType === "image/webp") return "screenshot.webp";
  if (contentType === "application/pdf") return "document.pdf";
  return "upload.bin";
}

function resolveSyncMode(request: Request): SyncMode {
  const url = new URL(request.url);
  const explicit = url.searchParams.get("sync");
  if (
    explicit === "events" ||
    explicit === "text" ||
    explicit === "markdown" ||
    explicit === "json"
  ) {
    return explicit;
  }

  const accept = request.headers.get("accept")?.toLowerCase() ?? "";
  if (accept.includes("text/plain")) return "text";
  if (accept.includes("text/markdown")) return "markdown";
  if (
    accept.includes("application/x-ndjson") ||
    accept.includes("text/event-stream")
  ) {
    return "events";
  }
  if (accept.includes("*/*") && !isJsonUpload(request)) return "text";
  return "async";
}

function isJsonUpload(request: Request): boolean {
  return (
    request.headers.get("content-type")?.includes("application/json") ?? false
  );
}

function streamTaskEvents(
  request: Request,
  service: TaskService,
  id: string,
  since: number,
  disconnectGraceMs: number,
): Response {
  const accept = request.headers.get("accept")?.toLowerCase() ?? "";
  const format = accept.includes("application/x-ndjson") ? "ndjson" : "sse";
  const encoder = new TextEncoder();
  const streamAbort = new AbortController();
  let disconnected = request.signal.aborted;
  let streamClosed = false;
  const abortStream = () => {
    disconnected = true;
    streamAbort.abort();
  };

  if (request.signal.aborted) {
    streamAbort.abort();
  } else {
    request.signal.addEventListener("abort", abortStream, { once: true });
  }

  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      const watcher = registerEventStreamWatcher(
        service,
        id,
        disconnectGraceMs,
      );
      let sawTerminal = false;
      try {
        for await (const event of service.watch(id, {
          since,
          signal: streamAbort.signal,
        })) {
          if (TERMINAL_EVENT_TYPES.has(event.type)) sawTerminal = true;
          if (streamClosed) break;
          controller.enqueue(
            encoder.encode(
              format === "ndjson" ? toNdjson(event) : toSse(event),
            ),
          );
        }
        if (!streamClosed) {
          streamClosed = true;
          controller.close();
        }
      } catch (error) {
        if (!streamClosed) {
          streamClosed = true;
          controller.error(error);
        }
      } finally {
        request.signal.removeEventListener("abort", abortStream);
        watcher.close({ disconnected, sawTerminal });
      }
    },
    cancel() {
      streamClosed = true;
      abortStream();
    },
  });

  return new Response(body, {
    headers:
      format === "ndjson"
        ? {
            "Content-Type": "application/x-ndjson",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
          }
        : {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
          },
  });
}

function eventsDisconnectGraceMs(env: Env): number {
  return (
    optionalNumberParam(env.TASK_EVENTS_DISCONNECT_GRACE_MS ?? null) ??
    DEFAULT_EVENTS_DISCONNECT_GRACE_MS
  );
}

function registerEventStreamWatcher(
  service: TaskService,
  id: string,
  disconnectGraceMs: number,
): { close(options: { disconnected: boolean; sawTerminal: boolean }): void } {
  const states = eventStreamStatesFor(service);
  const state = states.get(id) ?? { activeWatchers: 0 };
  states.set(id, state);
  state.activeWatchers += 1;
  if (state.timer) {
    clearTimeout(state.timer);
    state.timer = undefined;
  }

  let closed = false;
  return {
    close({ disconnected, sawTerminal }) {
      if (closed) return;
      closed = true;
      state.activeWatchers = Math.max(0, state.activeWatchers - 1);

      if (
        disconnected &&
        !sawTerminal &&
        state.activeWatchers === 0 &&
        isCancellableRecord(service.get(id))
      ) {
        scheduleEventStreamGraceCancel(service, id, state, disconnectGraceMs);
        return;
      }

      if (state.activeWatchers === 0 && !state.timer) states.delete(id);
    },
  };
}

function eventStreamStatesFor(
  service: TaskService,
): Map<string, EventStreamWatchState> {
  const existing = eventStreamWatchStates.get(service);
  if (existing) return existing;
  const states = new Map<string, EventStreamWatchState>();
  eventStreamWatchStates.set(service, states);
  eventStreamWatchStateMaps.add(states);
  return states;
}

function scheduleEventStreamGraceCancel(
  service: TaskService,
  id: string,
  state: EventStreamWatchState,
  disconnectGraceMs: number,
): void {
  state.timer = setTimeout(() => {
    state.timer = undefined;
    if (state.activeWatchers === 0 && isCancellableRecord(service.get(id))) {
      service.cancel(id);
    }
    if (state.activeWatchers === 0 && !state.timer) {
      eventStreamStatesFor(service).delete(id);
    }
  }, disconnectGraceMs);
  unrefTimer(state.timer);
}

function isCancellableRecord(record: TaskRecord | undefined): boolean {
  return record?.state === "queued" || record?.state === "running";
}

function clearEventStreamWatchStates(): void {
  for (const states of eventStreamWatchStateMaps) {
    for (const state of states.values()) {
      if (state.timer) clearTimeout(state.timer);
    }
    states.clear();
  }
  eventStreamWatchStateMaps.clear();
}

function unrefTimer(timer: ReturnType<typeof setTimeout>): void {
  if (typeof timer === "object" && "unref" in timer) {
    (timer as { unref(): void }).unref();
  }
}

function toNdjson(event: ExtractionEvent): string {
  return `${JSON.stringify(event)}\n`;
}

function toSse(event: ExtractionEvent): string {
  return `id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

function resolveSince(request: Request, url: URL): number {
  const lastEventId = optionalNumberParam(request.headers.get("last-event-id"));
  if (lastEventId !== undefined) return lastEventId + 1;
  return numberParam(url.searchParams.get("since"));
}

function listTasksResponse(url: URL, service: TaskService): Response {
  const state = taskStateParam(url.searchParams.get("state"));
  if (state === "invalid") {
    return error_response("Unsupported task state.", 400);
  }

  const limit = listLimitParam(url.searchParams.get("limit"));
  const tasks = service
    .list({ state, limit })
    .map((record) => serializeTaskRecord(record));

  return json_response({
    tasks,
    count: tasks.length,
    state: state ?? null,
    limit,
  });
}

function taskStateParam(
  value: string | null,
): TaskState | "invalid" | undefined {
  if (!value) return undefined;
  if (!VALID_TASK_STATES.has(value as TaskState)) return "invalid";
  return value as TaskState;
}

function listLimitParam(value: string | null): number {
  const parsed = optionalNumberParam(value);
  if (parsed === undefined) return DEFAULT_TASK_LIST_LIMIT;
  return Math.min(parsed, MAX_TASK_LIST_LIMIT);
}

function numberParam(value: string | null): number {
  return optionalNumberParam(value) ?? 0;
}

function optionalNumberParam(value: string | null): number | undefined {
  if (value === null || value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0
    ? Math.floor(parsed)
    : undefined;
}

function textResponse(
  value: string,
  status: number,
  contentType: string,
  headers: Record<string, string> = {},
): Response {
  const bytes = new TextEncoder().encode(value);
  return new Response(bytes, {
    status,
    headers: {
      "Content-Type": contentType,
      "Content-Length": String(bytes.byteLength),
      ...headers,
    },
  });
}

function base64Json(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function statusForError(error: ExtractionError): number {
  if (error.httpStatus) return error.httpStatus;
  if (error.code === "UPLOAD_TOO_LARGE") return 413;
  if (error.code === "UNSUPPORTED_INPUT") return 400;
  if (error.code === "CAPACITY_EXCEEDED") return 503;
  if (error.code.startsWith("WORKER_")) return 502;
  return 502;
}

function serializeTaskRecord(
  record: TaskRecord,
  since = 0,
): Record<string, unknown> {
  return {
    id: record.id,
    state: record.state,
    request: serializeRequest(record.request),
    events: record.events.filter((event) => event.sequence >= since),
    result: record.result,
    error: record.error,
    createdAt: record.createdAt,
    updatedAt: record.updatedAt,
  };
}

function serializeRequest(request: ExtractionRequest): Record<string, unknown> {
  return {
    filename: request.filename,
    engine: request.engine,
    profile: request.profile,
    pdfMode: request.pdfMode,
    source: request.source ? serializeSource(request.source) : undefined,
    budgets: request.budgets,
    privacy: request.privacy,
    contentType: request.contentType,
    language: request.language,
    pageHints: request.pageHints,
  };
}

function serializeSource(
  source: NonNullable<ExtractionRequest["source"]>,
): Record<string, unknown> {
  if (source.kind === "file") {
    return {
      kind: "file",
      name: source.file.name,
      size: source.file.size,
      type: source.file.type,
    };
  }
  if (source.kind === "screenshot")
    return { kind: "screenshot", size: source.png.byteLength };
  return source;
}

function matchTaskRoute(
  pathname: string,
):
  | { kind: "extractText" }
  | { kind: "collection" }
  | { kind: "item"; id: string }
  | { kind: "events"; id: string }
  | { kind: "cancel"; id: string }
  | null {
  if (pathname === "/api/extract/text") return { kind: "extractText" };
  if (pathname === "/api/tasks") return { kind: "collection" };
  const match = /^\/api\/tasks\/([^/]+)(?:\/(events|cancel))?$/.exec(pathname);
  if (!match) return null;
  const id = decodeURIComponent(match[1]);
  if (match[2] === "events") return { kind: "events", id };
  if (match[2] === "cancel") return { kind: "cancel", id };
  return { kind: "item", id };
}

class OcrStreamTaskExecutor implements WorkerExecutor {
  constructor(private readonly env: Env) {}

  async execute(
    request: ExtractionRequest,
    context: WorkerContext,
  ): Promise<ExtractionMeta> {
    const file = await fileFromRequest(request);
    const targetUrl = new URL(`${this.env.OCR_URL}/v1/convert/stream`);
    targetUrl.searchParams.set("engine_type", request.engine);
    if (request.profile)
      targetUrl.searchParams.set("pipeline_profile", request.profile);
    if (request.pdfMode)
      targetUrl.searchParams.set("pdf_mode", request.pdfMode);

    const form = new FormData();
    form.append("file", file, request.filename || file.name || "upload");

    let response: Response;
    try {
      response = await fetch(targetUrl, {
        method: "POST",
        body: form,
        signal: context.signal,
      });
    } catch (error) {
      if (context.signal.aborted) throw error;
      throw new TaskExecutionError({
        code: "WORKER_FAILED",
        message: error instanceof Error ? error.message : String(error),
        retryable: true,
        partial: false,
        httpStatus: 502,
      });
    }

    if (!response.ok) {
      throw new TaskExecutionError({
        code: statusCodeToErrorCode(response.status),
        message: await safeResponseMessage(response),
        retryable: response.status >= 500,
        partial: false,
        httpStatus: response.status,
      });
    }

    return await readBackendEvents(response, context);
  }
}

async function fileFromRequest(request: ExtractionRequest): Promise<File> {
  if (request.source?.kind === "file") return request.source.file;
  if (request.source?.kind === "screenshot") {
    return new File(
      [request.source.png],
      request.filename || "screenshot.png",
      {
        type: request.contentType || "image/png",
      },
    );
  }
  throw new TaskExecutionError({
    code: "UNSUPPORTED_INPUT",
    message: "Task source is not executable by the local OCR gateway.",
    retryable: false,
    partial: false,
    httpStatus: 400,
  });
}

async function readBackendEvents(
  response: Response,
  context: WorkerContext,
): Promise<ExtractionMeta> {
  if (!response.body) {
    throw new TaskExecutionError({
      code: "WORKER_PROTOCOL",
      message: "OCR stream has no body.",
      retryable: false,
      partial: false,
      httpStatus: 502,
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let sawPage = false;

  const consume = (line: string): ExtractionMeta | null => {
    if (!line.trim()) return null;
    let event: any;
    try {
      event = JSON.parse(line);
    } catch {
      throw new TaskExecutionError({
        code: "WORKER_PROTOCOL",
        message: "OCR stream contains malformed NDJSON.",
        retryable: false,
        partial: sawPage,
        httpStatus: 502,
      });
    }

    if (
      event.type === "progress" ||
      event.type === "page" ||
      event.type === "warning"
    ) {
      if (event.type === "page") sawPage = true;
      context.emit(toWorkerEvent(event));
      return null;
    }
    if (event.type === "complete")
      return event.meta && typeof event.meta === "object" ? event.meta : {};
    if (event.type === "error") {
      throw new TaskExecutionError({
        code: typeof event.code === "string" ? event.code : "WORKER_REPORTED",
        message: String(
          event.message ?? event.detail ?? "OCR worker reported an error.",
        ),
        retryable: Boolean(event.retryable),
        partial: sawPage,
        httpStatus: 502,
      });
    }
    throw new TaskExecutionError({
      code: "WORKER_PROTOCOL",
      message: "OCR stream contains an unknown event.",
      retryable: false,
      partial: sawPage,
      httpStatus: 502,
    });
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const lines = buffered.split("\n");
    buffered = lines.pop() ?? "";
    for (const line of lines) {
      const meta = consume(line);
      if (meta) return meta;
    }
  }
  buffered += decoder.decode();
  if (buffered.trim()) {
    const meta = consume(buffered);
    if (meta) return meta;
  }
  throw new TaskExecutionError({
    code: "WORKER_PROTOCOL",
    message: "OCR stream ended before completion.",
    retryable: true,
    partial: sawPage,
    httpStatus: 502,
  });
}

function eventTotalPages(event: any): number | undefined {
  const totalPages =
    typeof event.total_pages === "number"
      ? event.total_pages
      : event.totalPages;
  return typeof totalPages === "number" && Number.isFinite(totalPages)
    ? Math.max(1, Math.floor(totalPages))
    : undefined;
}

function toWorkerEvent(event: any): WorkerEventInput {
  if (event.type === "progress") {
    return {
      type: "progress",
      stage: String(event.stage ?? "ocr"),
      page: typeof event.page === "number" ? event.page : undefined,
      percent: typeof event.percent === "number" ? event.percent : undefined,
      totalPages: eventTotalPages(event),
    };
  }
  if (event.type === "page") {
    return {
      type: "page",
      page: typeof event.page === "number" ? event.page : 1,
      markdown: String(event.markdown ?? ""),
      totalPages: eventTotalPages(event),
    };
  }
  return {
    type: "warning",
    code: String(event.code ?? "OCR_WARNING"),
    message: String(event.message ?? event.detail ?? "OCR warning."),
  };
}

async function safeResponseMessage(response: Response): Promise<string> {
  try {
    const text = await response.text();
    if (!text) return `OCR backend returned HTTP ${response.status}.`;
    try {
      const payload = JSON.parse(text) as { detail?: unknown; error?: unknown };
      return String(
        payload.detail ??
          payload.error ??
          `OCR backend returned HTTP ${response.status}.`,
      );
    } catch {
      return text.slice(0, 512);
    }
  } catch {
    return `OCR backend returned HTTP ${response.status}.`;
  }
}

function statusCodeToErrorCode(status: number): string {
  if (status === 400) return "UNSUPPORTED_INPUT";
  if (status === 413) return "UPLOAD_TOO_LARGE";
  if (status === 503) return "CAPACITY_EXCEEDED";
  return "WORKER_FAILED";
}
