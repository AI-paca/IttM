import {
  InMemoryTaskQueue,
  InMemoryTaskStore,
  SequentialIds,
} from "./in-memory";
import type {
  ExtractionError,
  ExtractionEvent,
  ExtractionEventInput,
  ExtractionMeta,
  ExtractionRequest,
  ExtractionResult,
  IdGenerator,
  TaskQueue,
  TaskRecord,
  TaskState,
  TaskStore,
  WorkerEventInput,
  WorkerExecutor,
} from "./types";

const TERMINAL_STATES = new Set<TaskState>([
  "completed",
  "failed",
  "cancelled",
]);

export class TaskCapacityError extends Error {
  constructor() {
    super("Task queue capacity exceeded.");
    this.name = "TaskCapacityError";
  }
}

export class TaskExecutionError extends Error {
  constructor(readonly extractionError: ExtractionError) {
    super(extractionError.message);
    this.name = "TaskExecutionError";
  }
}

export class TaskService {
  private readonly controllers = new Map<string, AbortController>();
  private readonly subscribers = new Map<
    string,
    Set<(event: ExtractionEvent) => void>
  >();
  private running = 0;

  constructor(
    private readonly executor: WorkerExecutor,
    private readonly options: {
      maxQueued?: number;
      maxWorkers?: number;
      store?: TaskStore;
      queue?: TaskQueue;
      ids?: IdGenerator;
    } = {},
  ) {}

  create(request: ExtractionRequest): TaskRecord {
    const queue = this.queue;
    if (queue.size >= (this.options.maxQueued ?? 32)) {
      throw new TaskCapacityError();
    }

    const id = this.ids.next();
    const now = new Date().toISOString();
    const record: TaskRecord = {
      id,
      request,
      state: "queued",
      events: [],
      createdAt: now,
      updatedAt: now,
    };
    this.append(record, { type: "accepted", taskId: id });
    this.store.put(record);
    queue.enqueue(id);
    return record;
  }

  get(id: string): TaskRecord | undefined {
    return this.store.get(id);
  }

  list(options: { state?: TaskState; limit?: number } = {}): TaskRecord[] {
    const records = this.store.list().slice().reverse();
    const filtered = options.state
      ? records.filter((record) => record.state === options.state)
      : records;
    if (typeof options.limit !== "number") return filtered;
    return filtered.slice(0, Math.max(0, Math.floor(options.limit)));
  }

  watch(
    id: string,
    options: { since?: number; signal?: AbortSignal } = {},
  ): AsyncIterable<ExtractionEvent> {
    const record = this.store.get(id);
    if (!record) throw new Error(`Task ${id} was not found.`);

    return this.createEventStream(record, options);
  }

  async runNext(): Promise<string | null> {
    if (this.running >= (this.options.maxWorkers ?? 1)) return null;
    const id = this.queue.dequeue();
    if (!id) return null;

    const record = this.store.get(id);
    if (!record || record.state !== "queued") return null;

    const controller = new AbortController();
    this.controllers.set(id, controller);
    record.state = "running";
    this.running += 1;

    try {
      const meta = await this.executor.execute(record.request, {
        signal: controller.signal,
        emit: (event) => this.appendWorkerEvent(record, event),
      });
      if (controller.signal.aborted) {
        this.cancelRecord(record);
      } else if (!TERMINAL_STATES.has(record.state)) {
        record.state = "completed";
        record.result = this.buildResult(record, meta);
        this.append(record, { type: "complete", meta });
      }
    } catch (error) {
      if (controller.signal.aborted) {
        this.cancelRecord(record);
      } else {
        record.state = "failed";
        const extractionError = this.toExtractionError(error, record);
        record.error = extractionError;
        this.append(record, { type: "error", ...extractionError });
      }
    } finally {
      this.controllers.delete(id);
      this.running -= 1;
    }
    return id;
  }

  cancel(id: string): TaskRecord | undefined {
    const record = this.store.get(id);
    if (!record || TERMINAL_STATES.has(record.state)) return record;

    if (record.state === "queued") {
      this.queue.remove(id);
      this.cancelRecord(record);
      return record;
    }

    record.state = "cancelling";
    this.controllers.get(id)?.abort();
    return record;
  }

  private appendWorkerEvent(record: TaskRecord, event: WorkerEventInput): void {
    if (record.state !== "running") return;
    this.append(record, event);
  }

  private cancelRecord(record: TaskRecord): void {
    if (record.state === "cancelled") return;
    record.state = "cancelled";
    const error: ExtractionError = {
      code: "CANCELLED",
      message: "Task was cancelled.",
      retryable: false,
      partial: record.events.some((event) => event.type === "page"),
      httpStatus: 499,
    };
    record.error = error;
    this.append(record, { type: "error", ...error });
  }

  private append(record: TaskRecord, event: ExtractionEventInput): void {
    const sequenced = {
      ...event,
      sequence: record.events.length,
    } as ExtractionEvent;
    record.events.push(sequenced);
    record.updatedAt = new Date().toISOString();
    this.notify(record.id, sequenced);
  }

  private buildResult(
    record: TaskRecord,
    meta: ExtractionMeta,
  ): ExtractionResult {
    const pageEvents = record.events.filter((event) => event.type === "page");
    const warnings = record.events
      .filter((event) => event.type === "warning")
      .map((event) => ({ code: event.code, message: event.message }));

    return {
      taskId: record.id,
      markdown: pageEvents.map((event) => event.markdown).join("\n\n---\n\n"),
      meta,
      pages: pageEvents.length,
      partial: warnings.length > 0,
      warnings,
    };
  }

  private toExtractionError(
    error: unknown,
    record: TaskRecord,
  ): ExtractionError {
    if (error instanceof TaskExecutionError) {
      return {
        ...error.extractionError,
        partial:
          error.extractionError.partial ||
          record.events.some((event) => event.type === "page"),
      };
    }

    return {
      code: "WORKER_FAILED",
      message: error instanceof Error ? error.message : String(error),
      retryable: false,
      partial: record.events.some((event) => event.type === "page"),
      httpStatus: 502,
    };
  }

  private notify(id: string, event: ExtractionEvent): void {
    for (const subscriber of this.subscribers.get(id) ?? []) {
      subscriber(event);
    }
  }

  private createEventStream(
    record: TaskRecord,
    options: { since?: number; signal?: AbortSignal },
  ): AsyncIterable<ExtractionEvent> {
    const subscribersByTask = this.subscribers;
    return {
      async *[Symbol.asyncIterator]() {
        let nextSequence = options.since ?? 0;
        const pending: ExtractionEvent[] = record.events.filter(
          (event) => event.sequence >= nextSequence,
        );
        let wake: (() => void) | undefined;

        const subscriber = (event: ExtractionEvent) => {
          if (event.sequence < nextSequence) return;
          pending.push(event);
          wake?.();
          wake = undefined;
        };
        const abort = () => {
          wake?.();
          wake = undefined;
        };

        const subscribers =
          subscribersByTask.get(record.id) ?? new Set<typeof subscriber>();
        subscribers.add(subscriber);
        subscribersByTask.set(record.id, subscribers);
        options.signal?.addEventListener("abort", abort, { once: true });

        try {
          while (true) {
            while (pending.length) {
              const event = pending.shift()!;
              nextSequence = event.sequence + 1;
              yield event;
            }

            if (TERMINAL_STATES.has(record.state) || options.signal?.aborted) {
              return;
            }

            await new Promise<void>((resolve) => {
              wake = resolve;
            });
          }
        } finally {
          subscribers.delete(subscriber);
          if (!subscribers.size) subscribersByTask.delete(record.id);
          options.signal?.removeEventListener("abort", abort);
        }
      },
    };
  }

  private get store(): TaskStore {
    return this.options.store ?? (this.options.store = new InMemoryTaskStore());
  }

  private get queue(): TaskQueue {
    return this.options.queue ?? (this.options.queue = new InMemoryTaskQueue());
  }

  private get ids(): IdGenerator {
    return this.options.ids ?? (this.options.ids = new SequentialIds());
  }
}
