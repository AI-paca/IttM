export interface Env {
  PORT: string;
  OCR_URL: string;
  TASK_EVENTS_DISCONNECT_GRACE_MS?: string;
}

export interface ConvertMeta {
  engine: string;
  chunks: number;
  pages: number;
  elapsed_ms: number;
}

export interface ConvertResult {
  markdown: string;
  meta: ConvertMeta;
}

export interface CapabilityReport {
  runtime: any;
  hardware: any;
  engines: any;
  loaders: any;
}

export interface ProbeCaseResult {
  name: string;
  ok: boolean;
  message: string;
  elapsed_ms: number;
}

export interface ProbeReport {
  ok: boolean;
  cases: ProbeCaseResult[];
}

export interface ProbeRequest {
  modes: string[];
  engines: string[];
}
