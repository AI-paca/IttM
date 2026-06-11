import { Env } from "../domain/types";

function describeFetchError(error: any): string {
  const messages = [
    error?.message,
    error?.cause?.code,
    error?.cause?.message,
  ].filter(Boolean);
  return messages.length ? messages.join(": ") : String(error);
}

function backendFetchError(targetUrl: string, error: any): Response {
  return new Response(
    JSON.stringify({
      error: `Gateway to Python fetch error at ${targetUrl}: ${describeFetchError(error)}`,
    }),
    {
      status: 502,
      headers: { "content-type": "application/json" },
    },
  );
}

export class OcrClient {
  static async convert(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const queryParams = url.searchParams.toString();
    const targetUrl = `${env.OCR_URL}/v1/convert${queryParams ? "?" + queryParams : ""}`;

    try {
      const headers = new Headers();
      const contentType = req.headers.get("content-type");
      if (contentType) headers.set("content-type", contentType);

      return await fetch(targetUrl, {
        method: "POST",
        headers,
        body: req.body,
        // @ts-ignore
        duplex: "half",
      });
    } catch (e: any) {
      return backendFetchError(targetUrl, e);
    }
  }

  static async health(env: Env): Promise<Response> {
    const targetUrl = `${env.OCR_URL}/health`;
    try {
      return await fetch(targetUrl, { method: "GET" });
    } catch (e: any) {
      return backendFetchError(targetUrl, e);
    }
  }

  static async capabilities(env: Env): Promise<Response> {
    try {
      return await fetch(`${env.OCR_URL}/v1/capabilities`, { method: "GET" });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }

  static async probe(req: Request, env: Env): Promise<Response> {
    try {
      const data = await req.json();
      return await fetch(`${env.OCR_URL}/v1/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }

  static async diagnostics(env: Env): Promise<Response> {
    try {
      return await fetch(`${env.OCR_URL}/diagnostics`, { method: "GET" });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }

  static async installEasy(env: Env): Promise<Response> {
    try {
      return await fetch(`${env.OCR_URL}/v1/install-easyocr`, {
        method: "POST",
      });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }

  static async installEasyStatus(env: Env): Promise<Response> {
    try {
      return await fetch(`${env.OCR_URL}/v1/install-easyocr/status`, {
        method: "GET",
      });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }

  static async installLight(env: Env): Promise<Response> {
    try {
      return await fetch(`${env.OCR_URL}/v1/install-light`, { method: "POST" });
    } catch (e: any) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }
}
