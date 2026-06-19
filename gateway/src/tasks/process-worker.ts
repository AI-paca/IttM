import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type {
  ExtractionMeta,
  ExtractionRequest,
  WorkerContext,
  WorkerEventInput,
  WorkerExecutor,
} from "./types";

type WorkerMessage =
  | WorkerEventInput
  | { type: "complete"; meta?: ExtractionMeta }
  | {
      type: "error";
      code?: string;
      message?: string;
      retryable?: boolean;
    };

export class WorkerProcessError extends Error {
  constructor(
    readonly code:
      | "WORKER_ABORTED"
      | "WORKER_EXIT"
      | "WORKER_PROTOCOL"
      | "WORKER_REPORTED"
      | "WORKER_TIMEOUT",
    message: string,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "WorkerProcessError";
  }
}

export interface ProcessWorkerOptions {
  command: string;
  args?: string[];
  timeoutMs?: number;
  maxProtocolLineBytes?: number;
  maxStderrBytes?: number;
  spawnProcess?: (
    command: string,
    args: string[],
  ) => ChildProcessWithoutNullStreams;
}

export class ProcessWorkerExecutor implements WorkerExecutor {
  constructor(private readonly options: ProcessWorkerOptions) {}

  execute(
    request: ExtractionRequest,
    context: WorkerContext,
  ): Promise<ExtractionMeta> {
    const child = (this.options.spawnProcess ?? defaultSpawn)(
      this.options.command,
      this.options.args ?? [],
    );
    const timeoutMs = this.options.timeoutMs ?? 30_000;
    const maxLineBytes = this.options.maxProtocolLineBytes ?? 1024 * 1024;
    const maxStderrBytes = this.options.maxStderrBytes ?? 16 * 1024;

    return new Promise((resolve, reject) => {
      let settled = false;
      let stdoutBuffer = Buffer.alloc(0);
      let stderr = Buffer.alloc(0);

      const finish = (
        outcome:
          | { kind: "resolve"; meta: ExtractionMeta }
          | { kind: "reject"; error: Error },
      ) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        context.signal.removeEventListener("abort", abort);
        child.stdout.removeAllListeners();
        child.stderr.removeAllListeners();
        child.removeAllListeners();
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
        }
        if (outcome.kind === "resolve") resolve(outcome.meta);
        else reject(outcome.error);
      };

      const failProtocol = (message: string) => {
        finish({
          kind: "reject",
          error: new WorkerProcessError("WORKER_PROTOCOL", message),
        });
      };

      const consumeLine = (line: Buffer) => {
        if (!line.length) return;
        if (line.length > maxLineBytes) {
          failProtocol(`Worker protocol line exceeds ${maxLineBytes} bytes.`);
          return;
        }

        let message: WorkerMessage;
        try {
          message = JSON.parse(line.toString("utf8")) as WorkerMessage;
        } catch {
          failProtocol("Worker emitted malformed NDJSON.");
          return;
        }

        if (
          message.type === "progress" ||
          message.type === "page" ||
          message.type === "warning"
        ) {
          context.emit(message);
          return;
        }
        if (message.type === "complete") {
          finish({ kind: "resolve", meta: message.meta ?? {} });
          return;
        }
        if (message.type === "error") {
          finish({
            kind: "reject",
            error: new WorkerProcessError(
              "WORKER_REPORTED",
              message.message || "Worker reported an extraction failure.",
              message.retryable ?? false,
            ),
          });
          return;
        }
        failProtocol("Worker emitted an unknown protocol event.");
      };

      const timer = setTimeout(() => {
        finish({
          kind: "reject",
          error: new WorkerProcessError(
            "WORKER_TIMEOUT",
            `Worker exceeded the ${timeoutMs} ms time limit.`,
            true,
          ),
        });
      }, timeoutMs);
      timer.unref();

      const abort = () => {
        finish({
          kind: "reject",
          error: new WorkerProcessError(
            "WORKER_ABORTED",
            "Worker execution was cancelled.",
          ),
        });
      };
      context.signal.addEventListener("abort", abort, { once: true });

      child.stdout.on("data", (chunk: Buffer) => {
        stdoutBuffer = Buffer.concat([stdoutBuffer, chunk]);
        if (stdoutBuffer.length > maxLineBytes && !stdoutBuffer.includes(10)) {
          failProtocol(`Worker protocol line exceeds ${maxLineBytes} bytes.`);
          return;
        }
        let newline = stdoutBuffer.indexOf(10);
        while (newline >= 0 && !settled) {
          const line = stdoutBuffer.subarray(0, newline);
          stdoutBuffer = stdoutBuffer.subarray(newline + 1);
          consumeLine(line);
          newline = stdoutBuffer.indexOf(10);
        }
      });
      child.stderr.on("data", (chunk: Buffer) => {
        if (stderr.length >= maxStderrBytes) return;
        stderr = Buffer.concat([stderr, chunk]).subarray(0, maxStderrBytes);
      });
      child.on("error", (error) => {
        finish({
          kind: "reject",
          error: new WorkerProcessError("WORKER_EXIT", error.message, true),
        });
      });
      child.on("exit", (code, signal) => {
        if (settled) return;
        if (stdoutBuffer.length) consumeLine(stdoutBuffer);
        if (settled) return;
        const detail = stderr.toString("utf8").trim();
        finish({
          kind: "reject",
          error: new WorkerProcessError(
            "WORKER_EXIT",
            [
              `Worker exited before completion (code=${String(code)}, signal=${String(signal)}).`,
              detail,
            ]
              .filter(Boolean)
              .join(" "),
            true,
          ),
        });
      });

      if (context.signal.aborted) {
        abort();
        return;
      }
      child.stdin.end(`${JSON.stringify(request)}\n`);
    });
  }
}

function defaultSpawn(
  command: string,
  args: string[],
): ChildProcessWithoutNullStreams {
  return spawn(command, args, {
    stdio: ["pipe", "pipe", "pipe"],
  });
}
