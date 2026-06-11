interface Env {
  ORIGIN_URL: string;
  GEMINI_API_KEY?: string;
  ALLOWED_ORIGINS?: string;
  MAX_UPLOAD_BYTES?: string;
}

const DEFAULT_CORS_HEADERS = {
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers":
    "Content-Type, Authorization, X-Requested-With",
  "Access-Control-Max-Age": "86400",
  Vary: "Origin",
};

function getCorsHeaders(request: Request, env: Env): Record<string, string> {
  const origin = request.headers.get("Origin");
  const allowed = env.ALLOWED_ORIGINS
    ? env.ALLOWED_ORIGINS.split(",").map((item) => item.trim())
    : [];

  if (!origin) {
    return { ...DEFAULT_CORS_HEADERS, "Access-Control-Allow-Origin": "*" };
  }

  if (
    allowed.includes("*") ||
    allowed.includes(origin) ||
    origin.startsWith("moz-extension://") ||
    origin.startsWith("chrome-extension://")
  ) {
    return {
      ...DEFAULT_CORS_HEADERS,
      "Access-Control-Allow-Origin": origin,
    };
  }

  return { ...DEFAULT_CORS_HEADERS, "Access-Control-Allow-Origin": "null" };
}

function json(
  data: unknown,
  status: number,
  corsHeaders: Record<string, string>,
): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      ...corsHeaders,
      "content-type": "application/json; charset=utf-8",
    },
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

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const corsHeaders = getCorsHeaders(request, env);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: corsHeaders,
      });
    }

    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/gemini")) {
      if (!env.GEMINI_API_KEY) {
        return json(
          { error: "Gemini API key not configured on Edge" },
          503,
          corsHeaders,
        );
      }
      const targetUrl = new URL(
        url.pathname.replace("/api/gemini", ""),
        "https://generativelanguage.googleapis.com",
      );
      targetUrl.search = url.search;

      const proxyHeaders = new Headers(request.headers);
      proxyHeaders.delete("host");
      proxyHeaders.set("x-goog-api-key", env.GEMINI_API_KEY);

      const proxyReq = new Request(targetUrl, {
        method: request.method,
        headers: proxyHeaders,
        body: request.body,
      });

      const res = await fetch(proxyReq);
      const responseHeaders = new Headers(res.headers);
      Object.entries(corsHeaders).forEach(([k, v]) =>
        responseHeaders.set(k, v),
      );

      return new Response(res.body, {
        status: res.status,
        statusText: res.statusText,
        headers: responseHeaders,
      });
    }

    if (!env.ORIGIN_URL) {
      return json({ error: "ORIGIN_URL is not configured" }, 503, corsHeaders);
    }

    if (url.pathname.startsWith("/api/") || url.pathname === "/convert") {
      const length = contentLength(request);
      if (length !== null && length > maxUploadBytes(env)) {
        return json({ error: "Uploaded file is too large" }, 413, corsHeaders);
      }
    }

    const originUrl = new URL(env.ORIGIN_URL);
    originUrl.pathname = url.pathname;
    originUrl.search = url.search;

    const proxyHeaders = new Headers(request.headers);
    proxyHeaders.set("x-forwarded-host", url.host);
    proxyHeaders.set("x-forwarded-proto", url.protocol.replace(":", ""));

    const targetRequest = new Request(originUrl, {
      body: request.body,
      headers: proxyHeaders,
      method: request.method,
      redirect: "manual",
    });

    try {
      const originResponse = await fetch(targetRequest);
      const resHeaders = new Headers(originResponse.headers);
      Object.entries(corsHeaders).forEach(([k, v]) => resHeaders.set(k, v));

      return new Response(originResponse.body, {
        status: originResponse.status,
        statusText: originResponse.statusText,
        headers: resHeaders,
      });
    } catch {
      return json(
        { error: "Upstream (Origin) is unreachable / Tunnel down" },
        502,
        corsHeaders,
      );
    }
  },
};
