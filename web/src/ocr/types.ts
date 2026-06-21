export type { AppState } from "../types/app.types";
export type SourceType =
  | "auto"
  | "gateway"
  | "browser"
  | "local_tess"
  | "local_easy"
  | "llm";
export type LlmProvider = "gemini" | "openrouter";

export interface OcrResult {
  markdown: string;
  meta?: Record<string, unknown>;
}

export interface BrowserDiagnostics {
  memory: number | "Unknown";
  cores: number | "Unknown";
}

export interface BackendGpuInfo {
  type?: string;
  name: string;
  version?: string;
}

export interface BackendDiagnostics {
  python_version?: string;
  system?: string;
  memory_total_gb?: number;
  memory_used_gb?: number;
  cpu_cores?: number;
  gpus?: BackendGpuInfo[];
  gpu_error?: string | null;
  torch_error?: string | null;
  torch_available?: boolean;
  easyocr_available?: boolean;
  error?: string;
}

export interface AppDiagnostics {
  backend: BackendDiagnostics | null;
  browser: BrowserDiagnostics;
  error?: string;
}

export interface PlatformErrorShape {
  code?: string;
  message: string;
  status?: number;
  source: string;
  raw?: string;
  partialResult?: boolean;
}

export class PlatformError extends Error {
  code?: string;
  status?: number;
  source: string;
  raw?: string;
  partialResult: boolean;

  constructor(error: PlatformErrorShape) {
    super(error.message);
    this.name = "PlatformError";
    this.code = error.code;
    this.status = error.status;
    this.source = error.source;
    this.raw = error.raw;
    this.partialResult = Boolean(error.partialResult);
  }
}

export interface ProgressSink {
  (msg: string, percent?: number): void;
}
