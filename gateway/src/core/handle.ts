import { Env } from "../domain/types";
import { route } from "./routes";
import { error_response, not_found } from "./http";

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);

  if (!isGatewayApiRequest(url.pathname)) {
    return not_found();
  }

  try {
    return await route(request, env);
  } catch (err: any) {
    return error_response(err.message, 500);
  }
}

export function isGatewayApiRequest(pathname: string): boolean {
  return (
    pathname.startsWith("/api/") ||
    pathname === "/convert" ||
    pathname.startsWith("/convert/")
  );
}

export {
  error_response,
  json_response,
  method_not_allowed,
  not_found,
} from "./http";
