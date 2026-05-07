import * as http from "http";
import { handle } from "../core/handle";
import { Env } from "../domain/types";

export function read_node_env(): Env {
  return {
    PORT: process.env.PORT || "3000",
    OCR_URL: process.env.OCR_URL || "http://127.0.0.1:8000",
  };
}

export async function to_web_request(req: http.IncomingMessage): Promise<Request> {
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

export async function send_web_response(res: http.ServerResponse, webRes: Response) {
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

export function start_node() {
  const env = read_node_env();
  const server = http.createServer(async (req, res) => {
    try {
      const webReq = await to_web_request(req);
      const webRes = await handle(webReq, env);
      await send_web_response(res, webRes);
    } catch (err) {
      console.error("Node Adapter Error:", err);
      res.statusCode = 500;
      res.end("Internal Server Error");
    }
  });

  server.listen(parseInt(env.PORT), "0.0.0.0", () => {
    console.log(`Node adapter running on port ${env.PORT} (Fallback mode)`);
  });
}

// Automatically start if runs as main script
if (require.main === module) {
  start_node();
}
