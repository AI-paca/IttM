import { Env } from '../domain/types';
import { route } from './routes';
import * as fs from 'fs';
import * as path from 'path';

export async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);

  // API Routes
  if (url.pathname.startsWith('/api/')) {
    try {
      return await route(request, env);
    } catch (err: any) {
      return error_response(err.message, 500);
    }
  }

  // Static files serving fallback (for basic adapter usage without Vite)
  let staticPath = url.pathname === '/' ? '/index.html' : url.pathname;
  let filePath = path.join(process.cwd(), 'dist', staticPath);

  try {
    if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      // Fallback for SPA (if user requests a path like /configure)
      filePath = path.join(process.cwd(), 'dist', 'index.html');
    }

    if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
      const ext = path.extname(filePath);
      const contentTypes: Record<string, string> = {
        '.html': 'text/html',
        '.css': 'text/css',
        '.js': 'application/javascript',
        '.png': 'image/png',
        '.svg': 'image/svg+xml'
      };
      const contentType = contentTypes[ext] || 'application/octet-stream';
      const fileContent = fs.readFileSync(filePath);
      return new Response(fileContent, {
        headers: { 'Content-Type': contentType }
      });
    }
  } catch (err) {}

  return not_found();
}

export function not_found(): Response {
  return new Response(JSON.stringify({ error: 'Not Found' }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' }
  });
}

export function method_not_allowed(): Response {
  return new Response(JSON.stringify({ error: 'Method Not Allowed' }), {
    status: 405,
    headers: { 'Content-Type': 'application/json' }
  });
}

export function json_response(data: any, status: number = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}

export function error_response(message: string, status: number = 500): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}
