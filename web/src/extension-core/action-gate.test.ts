import test from "node:test";
import assert from "node:assert/strict";
import { InjectionRegistry, SingleFlight } from "./action-gate";

test("rage clicks share one OAuth or upload operation", async () => {
  const gate = new SingleFlight();
  let calls = 0;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });

  const clicks = Array.from({ length: 1000 }, () =>
    gate.run("save-to-drive", async () => {
      calls += 1;
      await pending;
      return "saved";
    }),
  );
  assert.equal(calls, 1);

  release();
  assert.deepEqual(new Set(await Promise.all(clicks)), new Set(["saved"]));
});

test("failed single-flight action can be retried after cleanup", async () => {
  const gate = new SingleFlight();
  let calls = 0;
  const action = async () => {
    calls += 1;
    if (calls === 1) throw new Error("offline");
    return "ok";
  };

  await assert.rejects(() => gate.run("save", action), /offline/);
  assert.equal(await gate.run("save", action), "ok");
});

test("mutation storms inject once and allow reinjection after removal", () => {
  const registry = new InjectionRegistry();
  let injections = 0;

  for (let index = 0; index < 10_000; index += 1) {
    registry.ensure("chat-toolbar", () => {
      injections += 1;
    });
  }
  assert.equal(injections, 1);

  registry.removed("chat-toolbar");
  assert.equal(
    registry.ensure("chat-toolbar", () => {
      injections += 1;
    }),
    true,
  );
  assert.equal(injections, 2);
});
