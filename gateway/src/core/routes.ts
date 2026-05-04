import { Env } from '../domain/types';
import { method_not_allowed, not_found, json_response } from './handle';
import { OcrClient } from '../clients/ocrClient';

export async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  if (path === '/api/convert') {
    if (method !== 'POST') return method_not_allowed();
    return await OcrClient.convert(request, env);
  }

  if (path === '/api/health') {
    if (method !== 'GET') return method_not_allowed();
    return await OcrClient.health(env);
  }

  if (path === '/api/capabilities') {
    if (method !== 'GET') return method_not_allowed();
    return await OcrClient.capabilities(env);
  }

  if (path === '/api/probe') {
    if (method !== 'POST') return method_not_allowed();
    return await OcrClient.probe(request, env);
  }

  return not_found();
}
