import assert from "node:assert/strict";
import test from "node:test";
import type { TextReviewerResponse } from "./text-reviewer-protocol";
import {
  releaseBrowserTextReviewer,
  reviewBrowserOcrCandidate,
} from "./text-reviewer";

interface WorkerState {
  created: number;
  receivedText: string;
}

function installWorkerMock(responseMode: "with-result" | "without-usage") {
  const state: WorkerState = { created: 0, receivedText: "" };
  const globals = globalThis as unknown as Record<string, unknown>;
  const previousWorker = globals.Worker;

  class MockWorker {
    onmessage: ((event: MessageEvent<TextReviewerResponse>) => void) | null =
      null;
    constructor() {
      state.created += 1;
    }
    postMessage(request: { id: number; candidate: { text: string } }) {
      if (responseMode === "without-usage") return;
      state.receivedText = request.candidate.text;
      queueMicrotask(() => {
        this.onmessage?.({
          data: { id: request.id, type: "result", isText: false },
        } as MessageEvent<TextReviewerResponse>);
      });
    }
    terminate() {}
  }

  globals.Worker = MockWorker as unknown as typeof Worker;

  return {
    state,
    restore() {
      if (previousWorker === undefined) {
        delete globals.Worker;
      } else {
        globals.Worker = previousWorker;
      }
      releaseBrowserTextReviewer();
    },
  };
}

test("tiny reviewer skips compact numeric/math/greek cells", async () => {
  const mock = installWorkerMock("without-usage");
  try {
    for (const text of ["5", "2/3", "π", "x+y"]) {
      assert.equal(await reviewBrowserOcrCandidate({ text }), true);
    }
    assert.equal(mock.state.created, 0);
  } finally {
    mock.restore();
  }
});

test("tiny reviewer runs model for short ambiguous text", async () => {
  const mock = installWorkerMock("with-result");
  try {
    assert.equal(await reviewBrowserOcrCandidate({ text: "abc" }), false);
    assert.equal(mock.state.created, 1);
    assert.equal(mock.state.receivedText, "abc");
  } finally {
    mock.restore();
  }
});
