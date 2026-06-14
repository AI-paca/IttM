import test from "node:test";
import assert from "node:assert/strict";
import { TaskCapacityError, TaskService } from "./task-service";
import { WorkerProcessError } from "./process-worker";
import type { WorkerExecutor } from "./types";

const request = {
  filename: "generated.png",
  engine: "tesseract" as const,
  profile: "backend_tesseract_standard",
};

test("task lifecycle emits accepted, progress, pages and complete in order", async () => {
  const executor: WorkerExecutor = {
    async execute(_request, context) {
      context.emit({ type: "progress", stage: "decode", percent: 0.2 });
      context.emit({ type: "page", page: 1, markdown: "first" });
      context.emit({ type: "page", page: 2, markdown: "second" });
      return { pages: 2 };
    },
  };
  const service = new TaskService(executor);
  const task = service.create(request);

  await service.runNext();

  assert.equal(task.state, "completed");
  assert.deepEqual(
    task.events.map((event) => event.type),
    ["accepted", "progress", "page", "page", "complete"],
  );
  assert.deepEqual(
    task.events.map((event) => event.sequence),
    [0, 1, 2, 3, 4],
  );
});

test("queued cancellation is immediate, terminal and idempotent", () => {
  const service = new TaskService({
    async execute() {
      throw new Error("must not run");
    },
  });
  const task = service.create(request);

  assert.equal(service.cancel(task.id)?.state, "cancelled");
  assert.equal(service.cancel(task.id)?.state, "cancelled");
  assert.equal(task.events.filter((event) => event.type === "error").length, 1);
});

test("running cancellation aborts the worker and preserves partial status", async () => {
  let started!: () => void;
  const running = new Promise<void>((resolve) => {
    started = resolve;
  });
  const executor: WorkerExecutor = {
    async execute(_request, context) {
      context.emit({ type: "page", page: 1, markdown: "partial" });
      started();
      await new Promise<void>((_resolve, reject) => {
        context.signal.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      });
      return {};
    },
  };
  const service = new TaskService(executor);
  const task = service.create(request);
  const execution = service.runNext();
  await running;

  service.cancel(task.id);
  await execution;

  assert.equal(task.state, "cancelled");
  const error = task.events.at(-1);
  assert.equal(error?.type, "error");
  if (error?.type === "error") {
    assert.equal(error.code, "CANCELLED");
    assert.equal(error.partial, true);
  }
});

test("worker crashes become typed errors without losing prior pages", async () => {
  const service = new TaskService({
    async execute(_request, context) {
      context.emit({ type: "page", page: 1, markdown: "safe partial" });
      throw new Error("worker exited with SIGKILL");
    },
  });
  const task = service.create(request);

  await service.runNext();

  assert.equal(task.state, "failed");
  const error = task.events.at(-1);
  assert.equal(error?.type, "error");
  if (error?.type === "error") {
    assert.equal(error.code, "WORKER_FAILED");
    assert.equal(error.partial, true);
    assert.match(error.message, /SIGKILL/);
  }
});

test("retryable worker exits preserve partial output metadata", async () => {
  const service = new TaskService({
    async execute(_request, context) {
      context.emit({ type: "page", page: 1, markdown: "safe partial" });
      throw new WorkerProcessError(
        "WORKER_EXIT",
        "worker exited before completion",
        true,
      );
    },
  });
  const task = service.create(request);

  await service.runNext();

  assert.equal(task.state, "failed");
  assert.equal(task.error?.code, "WORKER_EXIT");
  assert.equal(task.error?.retryable, true);
  assert.equal(task.error?.partial, true);
});

test("bounded queue rejects rapid-fire work instead of growing forever", () => {
  const service = new TaskService(
    {
      async execute() {
        return {};
      },
    },
    { maxQueued: 2 },
  );

  service.create(request);
  service.create(request);
  assert.throws(() => service.create(request), TaskCapacityError);
});

test("task listing returns newest records with state and limit filters", () => {
  const service = new TaskService({
    async execute() {
      return {};
    },
  });
  const first = service.create(request);
  const second = service.create(request);
  const third = service.create(request);

  service.cancel(first.id);

  assert.deepEqual(
    service.list({ limit: 2 }).map((record) => record.id),
    [third.id, second.id],
  );
  assert.deepEqual(
    service.list({ state: "queued" }).map((record) => record.id),
    [third.id, second.id],
  );
  assert.deepEqual(
    service.list({ state: "cancelled" }).map((record) => record.id),
    [first.id],
  );
});

test("task watchers unsubscribe after cancellation", async () => {
  const service = new TaskService({
    async execute() {
      return {};
    },
  });
  const task = service.create(request);
  const controller = new AbortController();
  const iterator = service
    .watch(task.id, { signal: controller.signal })
    [Symbol.asyncIterator]();

  assert.equal((await iterator.next()).value?.type, "accepted");
  controller.abort();
  assert.equal((await iterator.next()).done, true);

  const internals = service as unknown as {
    subscribers: Map<string, Set<unknown>>;
  };
  assert.equal(internals.subscribers.has(task.id), false);
});

test("worker concurrency never exceeds configured capacity", async () => {
  const releases: Array<() => void> = [];
  let active = 0;
  let peak = 0;
  const executor: WorkerExecutor = {
    async execute() {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise<void>((resolve) => releases.push(resolve));
      active -= 1;
      return {};
    },
  };
  const service = new TaskService(executor, {
    maxQueued: 5,
    maxWorkers: 2,
  });
  service.create(request);
  service.create(request);
  service.create(request);

  const first = service.runNext();
  const second = service.runNext();
  assert.equal(await service.runNext(), null);
  assert.equal(peak, 2);

  releases.splice(0).forEach((release) => release());
  await Promise.all([first, second]);
  const third = service.runNext();
  releases.splice(0).forEach((release) => release());
  await third;

  assert.equal(peak, 2);
});
