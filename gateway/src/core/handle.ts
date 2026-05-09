import { Env } from "../domain/types";
import { route } from "./routes";
import * as fs from "fs";
import * as path from "path";

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);

  // API Routes
  if (url.pathname.startsWith("/api/")) {
    try {
      return await route(request, env);
    } catch (err: any) {
      return error_response(err.message, 500);
    }
  }

  // Static files serving fallback (for basic adapter usage without Vite).
  // Accept GitHub Pages-prefixed builds locally too, e.g. /IttM/assets/app.js.
  const distRoot = path.resolve(process.cwd(), "dist");
  let pathname = decodeURIComponent(url.pathname);
  if (pathname === "/IttM") pathname = "/";
  if (pathname.startsWith("/IttM/")) pathname = pathname.slice("/IttM".length);

  const staticPath =
    pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  const isAssetRequest = path.extname(staticPath) !== "";
  let filePath = path.resolve(distRoot, staticPath);

  try {
    if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      if (isAssetRequest) return not_found();
      // Fallback for SPA (if user requests a path like /configure)
      filePath = path.join(distRoot, "index.html");
    }

    if (!filePath.startsWith(distRoot + path.sep) && filePath !== distRoot) {
      return not_found();
    }

    if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
      const ext = path.extname(filePath).toLowerCase();
      const contentTypes: Record<string, string> = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".json": "application/json",
        ".map": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".wasm": "application/wasm",
      };
      const contentType = contentTypes[ext] || "application/octet-stream";
      const fileContent = fs.readFileSync(filePath);
      return new Response(fileContent, {
        headers: { "Content-Type": contentType },
      });
    }
  } catch (err) {
    console.error("Static file handling error:", err);
  }

  return not_found();
}

export function not_found(): Response {
  return new Response(JSON.stringify({ error: "Not Found" }), {
    status: 404,
    headers: { "Content-Type": "application/json" },
  });
}

export function method_not_allowed(): Response {
  return new Response(JSON.stringify({ error: "Method Not Allowed" }), {
    status: 405,
    headers: { "Content-Type": "application/json" },
  });
}

export function json_response(data: any, status: number = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function error_response(
  message: string,
  status: number = 500,
): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
