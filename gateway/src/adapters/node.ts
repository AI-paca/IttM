import express, {
  type NextFunction,
  type Request as ExpressRequest,
  type Response as ExpressResponse,
} from "express";
import * as http from "http";
import * as path from "path";
import { fileURLToPath } from "url";
import { handle, isGatewayApiRequest } from "../core/handle";
import { error_response } from "../core/http";
import { Env } from "../domain/types";
import {
  defaultDistRoot,
  isStaticAssetPath,
  stripStaticBasePath,
} from "../services/staticFiles";

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

function sendNotFoundJson(res: ExpressResponse) {
  res.status(404);
  res.type("application/json");
  res.send(JSON.stringify({ error: "Not Found" }));
}

function apiMiddleware(env: Env) {
  return async (
    req: ExpressRequest,
    res: ExpressResponse,
    next: NextFunction,
  ) => {
    if (!isGatewayApiRequest(req.path)) {
      next();
      return;
    }

    try {
      const webReq = await to_web_request(req);
      const webRes = await handle(webReq, env);
      await send_web_response(res, webRes);
    } catch (err: any) {
      console.error("Node Adapter API Error:", err.stack || err);
      const response = error_response("Internal Server Error in Gateway", 500);
      await send_web_response(res, response);
    }
  };
}

function spaFallback(distRoot: string) {
  return (req: ExpressRequest, res: ExpressResponse) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      sendNotFoundJson(res);
      return;
    }

    const pathname = stripStaticBasePath(req.path);
    if (isStaticAssetPath(pathname)) {
      sendNotFoundJson(res);
      return;
    }

    res.sendFile(path.join(distRoot, "index.html"), (err) => {
      if (err) sendNotFoundJson(res);
    });
  };
}

export function create_node_app(env: Env, options: { distRoot?: string } = {}) {
  const app = express();
  const distRoot = path.resolve(options.distRoot ?? defaultDistRoot());
  const staticOptions = {
    fallthrough: true,
    index: "index.html",
  };

  app.use(apiMiddleware(env));
  app.use("/IttM", express.static(distRoot, staticOptions));
  app.use(express.static(distRoot, staticOptions));
  app.use(spaFallback(distRoot));

  return app;
}

export function start_node() {
  const env = read_node_env();
  const app = create_node_app(env);

  app.listen(parseInt(env.PORT), "0.0.0.0", () => {
    console.log(`Node adapter running on port ${env.PORT} (Fallback mode)`);
  });
}

const isMainModule = process.argv[1]
  ? path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
  : false;

if (isMainModule) {
  start_node();
}
