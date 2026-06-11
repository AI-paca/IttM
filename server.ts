import express, {
  type NextFunction,
  type Request as ExpressRequest,
  type Response as ExpressResponse,
} from "express";
import * as http from "http";
import * as path from "path";
import { fileURLToPath } from "url";
import { handle, isGatewayApiRequest } from "./gateway/src/core/handle";
import { error_response } from "./gateway/src/core/http";
import { Env } from "./gateway/src/domain/types";
import { defaultDistRoot } from "./gateway/src/services/staticFiles";

export function read_node_env(): Env {
  return {
    PORT: process.env.PORT || "3000",
    OCR_URL: process.env.OCR_URL || "http://127.0.0.1:8000",
  };
}

export async function to_web_request(
  req: http.IncomingMessage,
): Promise<Request> {
  const urlStr = `http://${req.headers.host || "localhost"}${req.url || "/"}`;
  const url = new URL(urlStr);
  const controller = new AbortController();
  req.on("aborted", () => controller.abort());

  const init: RequestInit = {
    method: req.method,
    headers: req.headers as HeadersInit,
    signal: controller.signal,
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    const stream = new ReadableStream({
      start(controller) {
        req.on("data", (chunk) => controller.enqueue(chunk));
        req.on("end", () => controller.close());
        req.on("error", (err) => controller.error(err));
      },
    });
    init.body = stream;
    // Node 18+ requires duplex: 'half' for Request streaming body
    (init as any).duplex = "half";
  }

  return new Request(url, init);
}

export async function send_web_response(
  res: http.ServerResponse,
  webRes: Response,
) {
  res.statusCode = webRes.status;
  res.statusMessage = webRes.statusText;

  webRes.headers.forEach((value, name) => {
    res.setHeader(name, value);
  });

  if (webRes.body) {
    const reader = webRes.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      res.write(value);
    }
  }
  res.end();
}

function apiMiddleware(env: Env) {
  return async (
    req: ExpressRequest,
    res: ExpressResponse,
    next: NextFunction,
  ) => {
    // Make sure we only catch API routes
    if (!isGatewayApiRequest(req.path)) {
      next();
      return;
    }

    const startedAt = Date.now();
    console.log(`[gateway] ${req.method} ${req.originalUrl}`);

    try {
      const webReq = await to_web_request(req);
      const webRes = await handle(webReq, env);
      await send_web_response(res, webRes);
      console.log(
        `[gateway] ${req.method} ${req.originalUrl} -> ${webRes.status} (${Date.now() - startedAt}ms)`,
      );
    } catch (err: any) {
      console.error("API Error:", err.stack || err);
      const response = error_response("Internal Server Error in Gateway", 500);
      await send_web_response(res, response);
      console.log(
        `[gateway] ${req.method} ${req.originalUrl} -> 500 (${Date.now() - startedAt}ms)`,
      );
    }
  };
}

async function startServer(): Promise<http.Server> {
  const app = express();
  const env = read_node_env();
  const PORT = parseInt(env.PORT);
  const server = http.createServer(app);

  // API routes first
  app.use(apiMiddleware(env));

  // Vite middleware for development
  if (process.env.NODE_ENV !== "production") {
    const { createServer: createViteServer } = await import("vite");
    const vite = await createViteServer({
      configFile: "web/vite.config.ts",
      server: { middlewareMode: true, hmr: { server } },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    // Production static serving
    const distPath = path.resolve(defaultDistRoot());
    app.use("/IttM", express.static(distPath, { fallthrough: true }));
    app.use(express.static(distPath, { index: "index.html" }));
    app.use((req: ExpressRequest, res: ExpressResponse) => {
      if (req.method !== "GET" && req.method !== "HEAD") {
        res.status(404).json({ error: "Not Found" });
        return;
      }

      res.sendFile(path.join(distPath, "index.html"), (err) => {
        if (err) res.status(404).json({ error: "Not Found" });
      });
    });
  }

  return await new Promise<http.Server>((resolve, reject) => {
    server.once("error", reject);
    server.once("listening", () => {
      console.log(
        `Universal Entry Point (server.ts) running on http://localhost:${PORT}`,
      );
      resolve(server);
    });
    server.listen(PORT, "0.0.0.0");
  });
}

const importMetaUrl =
  typeof import.meta.url === "string" ? import.meta.url : "";
const isMainModule = process.argv[1]
  ? !importMetaUrl ||
    path.resolve(process.argv[1]) === fileURLToPath(importMetaUrl)
  : false;

if (isMainModule) {
  startServer().catch((err) => {
    console.error("Error starting server:", err);
    process.exit(1);
  });
}
