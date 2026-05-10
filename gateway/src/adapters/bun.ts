import { handle, isGatewayApiRequest } from "../core/handle";
import { Env } from "../domain/types";
import { serveStaticFile } from "../services/staticFiles";

export function read_bun_env(): Env {
  // @ts-ignore
  return {
    PORT: process.env.PORT || "3000",
    OCR_URL: process.env.OCR_URL || "http://127.0.0.1:8000",
  };
}

export function start_bun() {
  const env = read_bun_env();
  let port = parseInt(env.PORT);
  const maxAttempts = 10;
  let lastError: any;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      // @ts-ignore
      Bun.serve({
        port,
        idleTimeout: 255,
        fetch(request: Request) {
          const url = new URL(request.url);
          if (isGatewayApiRequest(url.pathname)) {
            return handle(request, env);
          }
          return serveStaticFile(request);
        },
      });

      console.log(`Bun adapter running on port ${port} (Default mode)`);
      return;
    } catch (error: any) {
      if (error.code === "EADDRINUSE" || error.message?.includes("port")) {
        console.log(`Port ${port} in use, trying ${port + 1}...`);
        port++;
        lastError = error;
      } else {
        throw error;
      }
    }
  }
  throw lastError || new Error("Failed to find free port");
}

// @ts-ignore
if (import.meta.main) {
  start_bun();
}
