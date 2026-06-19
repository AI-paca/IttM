import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BoundedInputStorage, InputStorageError } from "./input-storage";

async function* stream(...values: string[]) {
  for (const value of values) {
    yield Buffer.from(value);
  }
}

async function withTempDirectory(
  callback: (directory: string) => Promise<void>,
) {
  const directory = await mkdtemp(join(tmpdir(), "ittm-storage-test-"));
  try {
    await callback(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

test("small inputs stay in bounded memory", async () => {
  await withTempDirectory(async (directory) => {
    const storage = new BoundedInputStorage({
      maxBytes: 64,
      memoryBytes: 16,
      directory,
    });

    const input = await storage.store("small.png", stream("hello", " world"));

    assert.equal(input.kind, "memory");
    assert.equal(input.size, 11);
    assert.equal(Buffer.from(await input.read()).toString(), "hello world");
    assert.deepEqual(await readdir(directory), []);
  });
});

test("large inputs spill incrementally without trusting the filename", async () => {
  await withTempDirectory(async (directory) => {
    const storage = new BoundedInputStorage({
      maxBytes: 64,
      memoryBytes: 4,
      directory,
      createId: () => "fixed",
    });

    const input = await storage.store(
      "../../../etc/passwd.pdf",
      stream("abcd", "efgh", "ijkl"),
    );

    assert.equal(input.kind, "spool");
    assert.equal(input.originalName, "../../../etc/passwd.pdf");
    assert.equal(input.path, join(directory, "ittm-input-fixed.bin"));
    assert.equal(Buffer.from(await input.read()).toString(), "abcdefghijkl");
    await input.cleanup();
    await input.cleanup();
    assert.deepEqual(await readdir(directory), []);
  });
});

test("size limit removes a partial spool file", async () => {
  await withTempDirectory(async (directory) => {
    const storage = new BoundedInputStorage({
      maxBytes: 8,
      memoryBytes: 2,
      directory,
      createId: () => "overflow",
    });

    await assert.rejects(
      storage.store("large.bin", stream("abcd", "efgh", "i")),
      (error: unknown) =>
        error instanceof InputStorageError && error.code === "INPUT_TOO_LARGE",
    );
    assert.deepEqual(await readdir(directory), []);
  });
});

test("abort removes a partial spool file", async () => {
  await withTempDirectory(async (directory) => {
    const controller = new AbortController();
    const storage = new BoundedInputStorage({
      maxBytes: 64,
      memoryBytes: 2,
      directory,
      createId: () => "aborted",
    });

    async function* interrupted() {
      yield Buffer.from("abcd");
      controller.abort();
      yield Buffer.from("efgh");
    }

    await assert.rejects(
      storage.store("cancelled.bin", interrupted(), controller.signal),
      (error: unknown) =>
        error instanceof InputStorageError && error.code === "INPUT_ABORTED",
    );
    assert.deepEqual(await readdir(directory), []);
  });
});

test("disk write failures are typed and clean partial files", async () => {
  await withTempDirectory(async (directory) => {
    const storage = new BoundedInputStorage({
      maxBytes: 64,
      memoryBytes: 0,
      directory,
      createId: () => "disk-failure",
      fs: {
        async mkdir() {},
        async open() {
          return {
            async write() {
              throw new Error("ENOSPC");
            },
            async close() {},
          } as never;
        },
        async readFile() {
          return Buffer.alloc(0);
        },
        async rm() {},
      },
    });

    await assert.rejects(
      storage.store("disk.bin", stream("payload")),
      (error: unknown) =>
        error instanceof InputStorageError &&
        error.code === "INPUT_IO" &&
        /ENOSPC/.test(error.message),
    );
  });
});
