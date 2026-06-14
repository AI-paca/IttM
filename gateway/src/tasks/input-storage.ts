import { randomUUID } from "node:crypto";
import { mkdir, open, readFile, rm, type FileHandle } from "node:fs/promises";
import { join } from "node:path";

export class InputStorageError extends Error {
  constructor(
    readonly code: "INPUT_ABORTED" | "INPUT_IO" | "INPUT_TOO_LARGE",
    message: string,
  ) {
    super(message);
    this.name = "InputStorageError";
  }
}

export interface StoredInput {
  readonly kind: "memory" | "spool";
  readonly originalName: string;
  readonly size: number;
  readonly path?: string;
  read(): Promise<Uint8Array>;
  cleanup(): Promise<void>;
}

interface StorageFs {
  mkdir(path: string): Promise<unknown>;
  open(path: string): Promise<FileHandle>;
  readFile(path: string): Promise<Buffer>;
  rm(path: string): Promise<void>;
}

export interface BoundedInputStorageOptions {
  maxBytes: number;
  memoryBytes: number;
  directory: string;
  createId?: () => string;
  fs?: StorageFs;
}

export class BoundedInputStorage {
  constructor(private readonly options: BoundedInputStorageOptions) {
    if (options.maxBytes <= 0) {
      throw new RangeError("maxBytes must be greater than zero.");
    }
    if (options.memoryBytes < 0 || options.memoryBytes > options.maxBytes) {
      throw new RangeError("memoryBytes must be between zero and maxBytes.");
    }
  }

  async store(
    originalName: string,
    chunks: AsyncIterable<Uint8Array>,
    signal?: AbortSignal,
  ): Promise<StoredInput> {
    const fs = this.options.fs ?? defaultFs;
    const memoryChunks: Buffer[] = [];
    let handle: FileHandle | undefined;
    let spoolPath: string | undefined;
    let size = 0;

    const ensureActive = () => {
      if (signal?.aborted) {
        throw new InputStorageError(
          "INPUT_ABORTED",
          "Input upload was cancelled.",
        );
      }
    };

    const ensureSpool = async () => {
      if (handle) return;
      await fs.mkdir(this.options.directory);
      spoolPath = join(
        this.options.directory,
        `ittm-input-${(this.options.createId ?? randomUUID)()}.bin`,
      );
      handle = await fs.open(spoolPath);
      for (const buffered of memoryChunks) {
        await handle.write(buffered);
      }
      memoryChunks.length = 0;
    };

    try {
      for await (const chunk of chunks) {
        ensureActive();
        const bytes = Buffer.from(chunk);
        size += bytes.length;
        if (size > this.options.maxBytes) {
          throw new InputStorageError(
            "INPUT_TOO_LARGE",
            `Input exceeds the ${this.options.maxBytes} byte limit.`,
          );
        }

        if (handle || size > this.options.memoryBytes) {
          await ensureSpool();
          await handle?.write(bytes);
        } else {
          memoryChunks.push(bytes);
        }
      }
      ensureActive();
      if (size === 0) {
        throw new InputStorageError("INPUT_IO", "Input upload is empty.");
      }
      await handle?.close();
      handle = undefined;
    } catch (error) {
      await handle?.close().catch(() => undefined);
      if (spoolPath) await fs.rm(spoolPath).catch(() => undefined);
      if (error instanceof InputStorageError) throw error;
      throw new InputStorageError(
        "INPUT_IO",
        error instanceof Error ? error.message : String(error),
      );
    }

    if (!spoolPath) {
      const bytes = Buffer.concat(memoryChunks);
      return {
        kind: "memory",
        originalName,
        size,
        async read() {
          return bytes;
        },
        async cleanup() {},
      };
    }

    let removed = false;
    return {
      kind: "spool",
      originalName,
      size,
      path: spoolPath,
      read: () => fs.readFile(spoolPath),
      async cleanup() {
        if (removed) return;
        removed = true;
        await fs.rm(spoolPath).catch(() => undefined);
      },
    };
  }
}

const defaultFs: StorageFs = {
  mkdir: (path) => mkdir(path, { recursive: true, mode: 0o700 }),
  open: (path) => open(path, "wx", 0o600),
  readFile,
  rm: (path) => rm(path, { force: true }),
};
