import { Env } from "../domain/types";
import { OcrClient } from "../clients/ocrClient";
import { json_response, method_not_allowed, not_found } from "./http";

export async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  // Support both /convert and /api/convert for compatibility
  if (path === "/api/convert" || path === "/convert") {
    if (method !== "POST") return method_not_allowed();
    return await OcrClient.convert(request, env);
  }

  if (path === "/api/health") {
    if (method !== "GET") return method_not_allowed();
    return await OcrClient.health(env);
  }

  if (path === "/api/capabilities") {
    if (method !== "GET") return method_not_allowed();
    return await OcrClient.capabilities(env);
  }

  if (path === "/api/diagnostics") {
    if (method !== "GET") return method_not_allowed();
    return await OcrClient.diagnostics(env);
  }

  if (path === "/api/install-easyocr") {
    if (method !== "POST") return method_not_allowed();
    return await OcrClient.installEasy(env);
  }

  if (path === "/api/install-easyocr/status") {
    if (method !== "GET") return method_not_allowed();
    return await OcrClient.installEasyStatus(env);
  }

  if (path === "/api/install-light") {
    if (method !== "POST") return method_not_allowed();
    return json_response(
      {
        error:
          "install-light is not implemented in this build. Use documented dependencies or /api/install-easyocr.",
      },
      501,
    );
  }

  if (path === "/api/probe") {
    if (method !== "POST") return method_not_allowed();
    return await OcrClient.probe(request, env);
  }

  return not_found();
}
