interface Env {
  ORIGIN_URL: string;
  MAX_UPLOAD_BYTES?: string;
}

function json(data: unknown, status: number): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function maxUploadBytes(env: Env): number {
  const configured = Number(env.MAX_UPLOAD_BYTES);
  return Number.isFinite(configured) && configured > 0
    ? configured
    : 32 * 1024 * 1024;
}

function contentLength(request: Request): number | null {
  const value = request.headers.get("content-length");
  if (!value) return null;

  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function originRequest(request: Request, env: Env): Request {
  const incomingUrl = new URL(request.url);
  const originUrl = new URL(env.ORIGIN_URL);
  originUrl.pathname = incomingUrl.pathname;
  originUrl.search = incomingUrl.search;

  const headers = new Headers(request.headers);
  headers.set("x-forwarded-host", incomingUrl.host);
  headers.set("x-forwarded-proto", incomingUrl.protocol.replace(":", ""));

  return new Request(originUrl, {
    body: request.body,
    headers,
    method: request.method,
    redirect: "manual",
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!env.ORIGIN_URL) {
      return json({ error: "ORIGIN_URL is not configured" }, 503);
    }

    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/") || url.pathname === "/convert") {
      const length = contentLength(request);
      if (length !== null && length > maxUploadBytes(env)) {
        return json({ error: "Uploaded file is too large" }, 413);
      }
    }

    return fetch(originRequest(request, env));
  },
};
