export type TaskState =
  | "queued"
  | "running"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled";

export type ExtractionEngine = "auto" | "tesseract" | "easyocr" | "browser";

export type ExtractionSource =
  | { kind: "file"; file: File }
  | { kind: "uploaded"; id: string; size: number }
  | { kind: "url"; url: string; method?: "GET" }
  | { kind: "screenshot"; png: ArrayBuffer }
  | { kind: "dom"; selector: string; allowlist: string[] };

export interface ExtractionBudgets {
  maxWallMs?: number;
  maxEncodedBytes?: number;
  maxDecodedPixels?: number;
  maxPages?: number;
  maxQueueWaitMs?: number;
}

export interface PrivacyPolicy {
  redactResult?: "off" | "mask" | "drop";
  redactTelemetry?: "off" | "mask";
  consentExternal?: boolean;
}

export interface ExtractionRequest {
  filename: string;
  engine: ExtractionEngine;
  profile?: string;
  source?: ExtractionSource;
  budgets?: ExtractionBudgets;
  privacy?: PrivacyPolicy;
  contentType?: string;
  language?: string;
  pageHints?: number[];
}

interface SequencedEvent {
  sequence: number;
}

export interface ExtractionMeta {
  engine?: string;
  profile?: string;
  pages?: number;
  chunks?: number;
  cardsFound?: number;
  tablesFound?: number;
  tableCells?: number;
  pipeline?: string;
  preprocessSteps?: string[];
  layoutSteps?: string[];
  elapsedMs?: number;
  elapsed_ms?: number;
  stageTimings?: Record<string, number>;
  resources?: { rssMb?: number; cpuMs?: number };
  [key: string]: unknown;
}

export interface ExtractionError {
  code: string;
  message: string;
  retryable: boolean;
  partial: boolean;
  stage?: string;
  httpStatus?: number;
  cause?:
    | { kind: "worker"; detail: string }
    | { kind: "protocol"; line?: number }
    | { kind: "upload"; size?: number; limit?: number }
    | { kind: "capacity"; queued?: number; limit?: number };
}

export interface ExtractionResult {
  taskId: string;
  markdown: string;
  meta: ExtractionMeta;
  pages: number;
  partial: boolean;
  warnings: { code: string; message: string }[];
}

export type ExtractionEvent =
  | (SequencedEvent & { type: "accepted"; taskId: string })
  | (SequencedEvent & {
      type: "progress";
      stage: string;
      page?: number;
      percent?: number;
    })
  | (SequencedEvent & {
      type: "page";
      page: number;
      markdown: string;
    })
  | (SequencedEvent & { type: "warning"; code: string; message: string })
  | (SequencedEvent & {
      type: "error";
      code: string;
      message: string;
      retryable: boolean;
      partial: boolean;
      stage?: string;
      httpStatus?: number;
      cause?: ExtractionError["cause"];
    })
  | (SequencedEvent & {
      type: "complete";
      meta: ExtractionMeta;
    });

type WithoutSequence<T> = T extends unknown ? Omit<T, "sequence"> : never;

export type ExtractionEventInput = WithoutSequence<ExtractionEvent>;

export interface TaskRecord {
  id: string;
  request: ExtractionRequest;
  state: TaskState;
  events: ExtractionEvent[];
  result?: ExtractionResult;
  error?: ExtractionError;
  createdAt?: string;
  updatedAt?: string;
}

export type WorkerEvent = Extract<
  ExtractionEvent,
  { type: "progress" | "page" | "warning" }
>;
export type WorkerEventInput = WithoutSequence<WorkerEvent>;

export interface WorkerContext {
  signal: AbortSignal;
  emit(event: WorkerEventInput): void;
}

export interface WorkerExecutor {
  execute(
    request: ExtractionRequest,
    context: WorkerContext,
  ): Promise<ExtractionMeta>;
}

export interface TaskStore {
  get(id: string): TaskRecord | undefined;
  put(record: TaskRecord): void;
  list(): TaskRecord[];
}

export interface TaskQueue {
  enqueue(id: string): void;
  dequeue(): string | undefined;
  remove(id: string): boolean;
  get size(): number;
}

export interface IdGenerator {
  next(): string;
}
