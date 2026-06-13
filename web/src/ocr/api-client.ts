import {
  PlatformError,
  type OcrResult,
  type PlatformErrorShape,
  type ProgressSink,
} from "./types";

type ErrorPayload = Record<string, unknown> | string | null;

function firstString(values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (Array.isArray(value)) {
      const nested = firstString(value);
      if (nested) return nested;
    }
    if (value && typeof value === "object") {
      const obj = value as Record<string, unknown>;
      const nested = firstString([
        obj.message,
        obj.msg,
        obj.detail,
        obj.error,
        obj.reason,
      ]);
      if (nested) return nested;
    }
  }
  return null;
}

function stripHtml(input: string): string {
  const title = input.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1];
  const body = input.match(/<body[^>]*>([\s\S]*?)<\/body>/i)?.[1] ?? input;
  const candidate = title || body;

  return candidate
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ")
    .trim();
}

function payloadMessage(payload: ErrorPayload): string | null {
  if (!payload) return null;
  if (typeof payload === "string") return payload.trim() || null;
  return firstString([
    payload.detail,
    payload.error,
    payload.message,
    payload.reason,
    payload.errors,
  ]);
}

export function normalizePlatformError(
  error: unknown,
  source = "app",
): PlatformError {
  if (error instanceof PlatformError) return error;
  if (error instanceof Error) {
    return new PlatformError({
      message: error.message || "Неизвестная ошибка.",
      source,
    });
  }
  if (typeof error === "string") {
    return new PlatformError({ message: error, source });
  }
  if (error && typeof error === "object") {
    const message = firstString([
      (error as Record<string, unknown>).message,
      (error as Record<string, unknown>).detail,
      (error as Record<string, unknown>).error,
      (error as Record<string, unknown>).reason,
    ]);
    if (message) {
      return new PlatformError({ message, source });
    }
  }
  return new PlatformError({ message: "Неизвестная ошибка.", source });
}

export async function parsePlatformError(
  response: Response,
  source = "api",
): Promise<PlatformError> {
  const contentType = response.headers.get("content-type") || "";
  let raw = "";
  let parsed: ErrorPayload = null;

  try {
    raw = await response.text();
  } catch {
    raw = "";
  }

  if (raw && contentType.includes("application/json")) {
    try {
      parsed = JSON.parse(raw) as ErrorPayload;
    } catch {
      parsed = raw;
    }
  } else if (raw.trim().startsWith("{") || raw.trim().startsWith("[")) {
    try {
      parsed = JSON.parse(raw) as ErrorPayload;
    } catch {
      parsed = raw;
    }
  } else if (raw) {
    parsed =
      contentType.includes("html") || /<html|<body|<title/i.test(raw)
        ? stripHtml(raw)
        : raw;
  }

  const message =
    payloadMessage(parsed) ||
    response.statusText ||
    `Платформа вернула ошибку ${response.status}.`;

  return new PlatformError({
    message: `${source}: ${message}`,
    status: response.status,
    source,
    raw: raw.slice(0, 2000),
  });
}

export async function readJsonOrThrow<T>(
  response: Response,
  source: string,
): Promise<T> {
  if (!response.ok) throw await parsePlatformError(response, source);

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    throw new PlatformError({
      message: `${source}: ответ не является JSON (${normalizePlatformError(error).message})`,
      status: response.status,
      source,
    });
  }

  if (payload && typeof payload === "object") {
    const message = payloadMessage(payload as Record<string, unknown>);
    if (message && ("error" in payload || "detail" in payload)) {
      throw new PlatformError({
        message: `${source}: ${message}`,
        status: response.status,
        source,
        raw: JSON.stringify(payload).slice(0, 2000),
      });
    }
  }

  return payload as T;
}

export async function requestApiJson<T>(
  url: string,
  source: string,
  init?: RequestInit,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    throw new PlatformError({
      message: `${source}: сеть недоступна или запрос был заблокирован (${normalizePlatformError(error).message})`,
      source,
    });
  }
  return readJsonOrThrow<T>(response, source);
}

type ImportMetaWithEnv = ImportMeta & {
  env?: Record<string, string | undefined>;
};

export interface BackendGatewayCandidate {
  label: string;
  baseUrl: string;
}

function envValue(name: string): string {
  return ((import.meta as ImportMetaWithEnv).env?.[name] ?? "").trim();
}

export function parseGatewayUrlList(raw: string): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];

  for (const candidate of raw.split(/[\n,]+/)) {
    const trimmed = candidate.trim().replace(/\/+$/, "");
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    urls.push(trimmed);
  }

  return urls;
}

export function configuredCloudGatewayUrls(): string[] {
  return parseGatewayUrlList(envValue("VITE_CLOUD_OCR_URLS"));
}

export function buildBackendGatewayCandidates({
  customBaseUrl = "",
  includeCloud = true,
  includeLocal = true,
  cloudBaseUrls = configuredCloudGatewayUrls(),
}: {
  customBaseUrl?: string;
  includeCloud?: boolean;
  includeLocal?: boolean;
  cloudBaseUrls?: string[];
} = {}): BackendGatewayCandidate[] {
  const candidates: BackendGatewayCandidate[] = [];
  const seen = new Set<string>();

  const add = (label: string, baseUrl: string) => {
    const normalized = baseUrl.trim().replace(/\/+$/, "");
    const key = normalized || "__local__";
    if (seen.has(key)) return;
    seen.add(key);
    candidates.push({ label, baseUrl: normalized });
  };

  if (includeCloud) {
    cloudBaseUrls.forEach((baseUrl, index) => {
      add(index === 0 ? "Cloud OCR" : `Cloud OCR ${index + 1}`, baseUrl);
    });
  }

  if (customBaseUrl.trim()) add("Custom Gateway", customBaseUrl);
  if (includeLocal) add("Local Gateway", "");

  return candidates;
}

export function buildApiUrl(
  baseUrl: string,
  route: string,
  params?: Record<string, string>,
): string {
  const normalizedRoute = route.startsWith("/") ? route : `/${route}`;
  const trimmed = baseUrl.trim();
  let url: URL;

  if (!trimmed) {
    const origin =
      typeof window === "undefined"
        ? "http://localhost"
        : window.location.origin;
    url = new URL(normalizedRoute, origin);
    const relative = `${url.pathname}${url.search}`;
    if (params) {
      const search = new URLSearchParams(params).toString();
      return search ? `${relative}?${search}` : relative;
    }
    return relative;
  }

  const base = trimmed.endsWith("/") ? trimmed : `${trimmed}/`;
  url = new URL(base);
  const path = url.pathname.replace(/\/$/, "");
  const routeWithoutApi = normalizedRoute.replace(/^\/api/, "");
  url.pathname = path.endsWith("/api")
    ? `${path}${routeWithoutApi}`
    : path.endsWith(normalizedRoute)
      ? path
      : `${path}${normalizedRoute}`;
  if (params) {
    for (const [key, value] of Object.entries(params))
      url.searchParams.set(key, value);
  }
  return url.toString();
}

export function isOllamaBaseUrl(baseUrl: string): boolean {
  const trimmed = baseUrl.trim();
  if (!trimmed) return false;

  try {
    const url = new URL(trimmed);
    return (
      url.port === "11434" ||
      url.pathname === "/api/generate" ||
      url.pathname.endsWith("/api/generate")
    );
  } catch {
    return false;
  }
}

export function buildOllamaGenerateUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim() || "http://localhost:11434";
  const base = trimmed.endsWith("/") ? trimmed : `${trimmed}/`;
  const url = new URL(base);
  const path = url.pathname.replace(/\/$/, "");

  if (path.endsWith("/api/generate")) {
    url.pathname = path;
  } else if (path.endsWith("/api")) {
    url.pathname = `${path}/generate`;
  } else {
    url.pathname = `${path}/api/generate`;
  }

  return url.toString();
}

export async function executeBackendOcrWithFallback(
  targetFile: File,
  candidates: BackendGatewayCandidate[],
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
  params?: Record<string, string>,
  onChunk?: (text: string, pageIndex?: number) => void,
): Promise<OcrResult> {
  let lastError: unknown = new PlatformError({
    message: "Нет доступных OCR gateway endpoint-ов.",
    source: "OCR API",
  });

  for (const candidate of candidates) {
    if (!activeContent.current) break;

    const url = buildApiUrl(candidate.baseUrl, "/api/convert/stream", params);
    try {
      onProgress?.(`Пробуем ${candidate.label}...`);
      return await executeBackendOcrStreaming(
        targetFile,
        url,
        activeContent,
        onProgress,
        onChunk,
      );
    } catch (error) {
      lastError = error;
      if (normalizePlatformError(error).partialResult) throw error;
    }
  }

  throw lastError;
}

export async function executeBackendOcr(
  targetFile: File,
  url: string,
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
): Promise<OcrResult> {
  const formData = new FormData();
  formData.append("file", targetFile);
  if (activeContent.current) onProgress?.("Отправка на сервер...");

  let response: Response;
  try {
    response = await fetch(url, { method: "POST", body: formData });
  } catch (error) {
    throw new PlatformError({
      message: `OCR API: сеть недоступна или CORS/платформа заблокировали запрос (${normalizePlatformError(error).message})`,
      source: "OCR API",
    });
  }

  return readJsonOrThrow<OcrResult>(response, "OCR API");
}

interface BackendStreamPageEvent {
  type: "page";
  page: number;
  markdown: string;
}

interface BackendStreamCompleteEvent {
  type: "complete";
  meta: Record<string, unknown>;
}

interface BackendStreamErrorEvent {
  type: "error";
  detail?: string;
  error?: string;
}

type BackendStreamEvent =
  | BackendStreamPageEvent
  | BackendStreamCompleteEvent
  | BackendStreamErrorEvent;

function backendJsonUrl(streamUrl: string): string {
  return streamUrl.replace(/\/convert\/stream(?=[?#]|$)/, "/convert");
}

export async function readBackendOcrStream(
  response: Response,
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
  onChunk?: (text: string, pageIndex?: number) => void,
): Promise<OcrResult> {
  if (!response.ok) throw await parsePlatformError(response, "OCR API");
  if (!response.body) {
    throw new PlatformError({
      message: "OCR API: потоковый ответ не содержит body.",
      status: response.status,
      source: "OCR API",
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const markdownParts: string[] = [];
  let meta: Record<string, unknown> | undefined;
  let buffered = "";

  const consumeLine = (line: string) => {
    if (!line.trim()) return;
    let event: BackendStreamEvent;
    try {
      event = JSON.parse(line) as BackendStreamEvent;
    } catch (error) {
      throw new PlatformError({
        message: `OCR API: повреждённая строка NDJSON (${normalizePlatformError(error).message}).`,
        source: "OCR API",
        raw: line.slice(0, 2000),
      });
    }

    if (event.type === "error") {
      throw new PlatformError({
        message: `OCR API: ${event.detail || event.error || "неизвестная ошибка потока."}`,
        source: "OCR API",
      });
    }
    if (event.type === "complete") {
      meta = event.meta;
      return;
    }
    if (event.type !== "page" || typeof event.markdown !== "string") {
      throw new PlatformError({
        message: "OCR API: неизвестное событие потока.",
        source: "OCR API",
        raw: line.slice(0, 2000),
      });
    }

    markdownParts.push(event.markdown);
    onProgress?.(`Получена страница ${event.page}...`);
    onChunk?.(`${event.markdown}\n\n---\n\n`, event.page);
  };

  try {
    while (activeContent.current) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      const lines = buffered.split("\n");
      buffered = lines.pop() || "";
      lines.forEach(consumeLine);
    }
    buffered += decoder.decode();
    if (buffered.trim()) consumeLine(buffered);
  } catch (error) {
    const normalized = normalizePlatformError(error, "OCR API");
    if (markdownParts.length > 0) {
      throw new PlatformError({
        message: normalized.message,
        status: normalized.status,
        source: normalized.source,
        raw: normalized.raw,
        partialResult: true,
      });
    }
    throw error;
  } finally {
    if (!activeContent.current) await reader.cancel();
    reader.releaseLock();
  }

  if (activeContent.current && !meta) {
    throw new PlatformError({
      message: "OCR API: поток завершился до события complete.",
      source: "OCR API",
      partialResult: markdownParts.length > 0,
    });
  }
  return {
    markdown: markdownParts.join("\n\n---\n\n"),
    ...(meta ? { meta } : {}),
  };
}

export async function executeBackendOcrStreaming(
  targetFile: File,
  url: string,
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
  onChunk?: (text: string, pageIndex?: number) => void,
): Promise<OcrResult> {
  const formData = new FormData();
  formData.append("file", targetFile);
  if (activeContent.current) onProgress?.("Отправка на сервер...");

  let response: Response;
  try {
    response = await fetch(url, { method: "POST", body: formData });
  } catch (error) {
    throw new PlatformError({
      message: `OCR API: сеть недоступна или CORS/платформа заблокировали запрос (${normalizePlatformError(error).message})`,
      source: "OCR API",
    });
  }

  if (response.status === 404 || response.status === 405) {
    const result = await executeBackendOcr(
      targetFile,
      backendJsonUrl(url),
      activeContent,
      onProgress,
    );
    if (result.markdown) onChunk?.(result.markdown);
    return result;
  }
  return readBackendOcrStream(response, activeContent, onProgress, onChunk);
}

export function noticeFromError(error: unknown): PlatformErrorShape {
  const normalized = normalizePlatformError(error);
  return {
    message: normalized.message,
    status: normalized.status,
    source: normalized.source,
    raw: normalized.raw,
    partialResult: normalized.partialResult,
  };
}
