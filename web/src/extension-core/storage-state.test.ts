import test from "node:test";
import assert from "node:assert/strict";
import {
  readStoredJson,
  saveWithDiagnosticCleanup,
  type KeyValueStorage,
} from "./storage-state";

class MemoryStorage implements KeyValueStorage {
  readonly values = new Map<string, string>();
  failWrites = 0;
  readonly removed: string[] = [];

  async get(key: string): Promise<string | undefined> {
    return this.values.get(key);
  }

  async set(key: string, value: string): Promise<void> {
    if (this.failWrites > 0) {
      this.failWrites -= 1;
      throw new DOMException("quota", "QuotaExceededError");
    }
    this.values.set(key, value);
  }

  async remove(key: string): Promise<void> {
    this.removed.push(key);
    this.values.delete(key);
  }
}

test("corrupt state from an older extension version degrades to empty", async () => {
  const storage = new MemoryStorage();
  storage.values.set("session", "{broken-json");

  assert.equal(await readStoredJson(storage, "session"), null);
});

test("quota exhaustion clears diagnostics and retries canonical state once", async () => {
  const storage = new MemoryStorage();
  storage.values.set("diagnostics", "large logs");
  storage.failWrites = 1;

  await saveWithDiagnosticCleanup(storage, "draft", { text: "safe" });

  assert.deepEqual(storage.removed, ["diagnostics"]);
  assert.deepEqual(await readStoredJson(storage, "draft"), { text: "safe" });
});

test("persistent storage failure is surfaced instead of silently swallowed", async () => {
  const storage = new MemoryStorage();
  storage.failWrites = 2;

  await assert.rejects(
    () => saveWithDiagnosticCleanup(storage, "draft", { text: "safe" }),
    (error: unknown) =>
      error instanceof DOMException && error.name === "QuotaExceededError",
  );
});
