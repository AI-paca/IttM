import * as fs from "fs/promises";
import * as path from "path";
import { charset, lookup } from "mime-types";
import { not_found } from "../core/http";

export interface StaticFileOptions {
  distRoot?: string;
}

const pagesBasePath = "/IttM";

export function defaultDistRoot(): string {
  return path.resolve(process.cwd(), "dist");
}

export function stripStaticBasePath(pathname: string): string {
  if (pathname === pagesBasePath) return "/";
  if (pathname.startsWith(`${pagesBasePath}/`)) {
    return pathname.slice(pagesBasePath.length);
  }
  return pathname;
}

export function isStaticAssetPath(pathname: string): boolean {
  return path.extname(stripStaticBasePath(pathname)) !== "";
}

function contentTypeFor(filePath: string): string {
  const mimeType = lookup(filePath) || "application/octet-stream";
  const detectedCharset = charset(mimeType);
  return detectedCharset ? `${mimeType}; charset=${detectedCharset}` : mimeType;
}

function safeResolve(root: string, relativePath: string): string | null {
  const resolvedRoot = path.resolve(root);
  const resolvedPath = path.resolve(resolvedRoot, relativePath);
  const relative = path.relative(resolvedRoot, resolvedPath);

  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    return null;
  }
  return resolvedPath;
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    return (await fs.stat(filePath)).isFile();
  } catch {
    return false;
  }
}

export async function serveStaticFile(
  request: Request,
  options: StaticFileOptions = {},
): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return not_found();
  }

  const distRoot = path.resolve(options.distRoot ?? defaultDistRoot());
  const url = new URL(request.url);
  let pathname: string;

  try {
    pathname = stripStaticBasePath(decodeURIComponent(url.pathname));
  } catch {
    return not_found();
  }

  const relativePath =
    pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  const assetRequest = isStaticAssetPath(pathname);
  let filePath = safeResolve(distRoot, relativePath);

  if (!filePath || !(await fileExists(filePath))) {
    if (assetRequest) return not_found();
    filePath = safeResolve(distRoot, "index.html");
  }

  if (!filePath || !(await fileExists(filePath))) {
    return not_found();
  }

  return new Response(
    request.method === "HEAD" ? null : await fs.readFile(filePath),
    {
      headers: { "Content-Type": contentTypeFor(filePath) },
    },
  );
}
