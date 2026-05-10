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

interface ImportMetaWithEnv extends ImportMeta {
  env?: Record<string, string | undefined>;
}

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

export async function executeBackendOcrWithFallback(
  targetFile: File,
  candidates: BackendGatewayCandidate[],
  activeContent: { current: boolean },
  onProgress?: ProgressSink,
  params?: Record<string, string>,
): Promise<OcrResult> {
  let lastError: unknown = new PlatformError({
    message: "Нет доступных OCR gateway endpoint-ов.",
    source: "OCR API",
  });

  for (const candidate of candidates) {
    if (!activeContent.current) break;

    const url = buildApiUrl(candidate.baseUrl, "/api/convert", params);
    try {
      onProgress?.(`Пробуем ${candidate.label}...`);
      return await executeBackendOcr(
        targetFile,
        url,
        activeContent,
        onProgress,
      );
    } catch (error) {
      lastError = error;
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

export function noticeFromError(error: unknown): PlatformErrorShape {
  const normalized = normalizePlatformError(error);
  return {
    message: normalized.message,
    status: normalized.status,
    source: normalized.source,
    raw: normalized.raw,
  };
}
