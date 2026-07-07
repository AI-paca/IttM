import type {
  BrowserTextCandidate,
  TextReviewerRequest,
  TextReviewerResponse,
} from "./text-reviewer-protocol";

interface PendingReview {
  resolve: (isText: boolean) => void;
}

let worker: Worker | null = null;
let nextRequestId = 1;
const pending = new Map<number, PendingReview>();

function shouldUseModel(candidate: BrowserTextCandidate): boolean {
  const text = candidate.text.trim();
  if (!text) return false;
  if (/^[\p{N}+\-*/=<>^_√∫ΣΠπ∞≈≠≤≥.,:%()№]+$/u.test(text)) {
    return false;
  }

  const characters = Array.from(text);
  const letters = characters.filter((character) => /\p{L}/u.test(character));
  const unusual = characters.filter(
    (character) =>
      !/[\p{L}\p{N}\s]/u.test(character) &&
      !".,;:!?+-*/=<>^_()[]{}№%$€₽'\"".includes(character),
  );
  const scripts = [
    /[A-Za-z]/,
    /[\u0400-\u04ff]/,
    /[\u0370-\u03ff]/,
    /[\u3400-\u9fff]/,
  ].filter((pattern) => pattern.test(text)).length;
  return (
    letters.length <= 3 ||
    unusual.length > 0 ||
    scripts > 1 ||
    (typeof candidate.confidence === "number" && candidate.confidence < 45)
  );
}

function reviewerWorker(): Worker | null {
  if (typeof Worker === "undefined") return null;
  if (worker) return worker;

  worker = new Worker(new URL("./text-reviewer.worker.ts", import.meta.url), {
    type: "module",
  });
  worker.onmessage = (event: MessageEvent<TextReviewerResponse>) => {
    const response = event.data;
    const review = pending.get(response.id);
    if (!review) return;
    pending.delete(response.id);
    review.resolve(
      response.type === "result" && typeof response.isText === "boolean"
        ? response.isText
        : true,
    );
  };
  worker.onerror = () => {
    for (const review of pending.values()) review.resolve(true);
    pending.clear();
    worker?.terminate();
    worker = null;
  };
  return worker;
}

export async function reviewBrowserOcrCandidate(
  candidate: BrowserTextCandidate,
): Promise<boolean> {
  if (!candidate.text.trim()) return false;
  if (!shouldUseModel(candidate)) return true;

  const activeWorker = reviewerWorker();
  if (!activeWorker) return true;
  const id = nextRequestId++;
  const request: TextReviewerRequest = {
    id,
    type: "review",
    candidate,
  };
  return await new Promise<boolean>((resolve) => {
    pending.set(id, { resolve });
    activeWorker.postMessage(request);
  });
}

export function releaseBrowserTextReviewer(): void {
  for (const review of pending.values()) review.resolve(true);
  pending.clear();
  worker?.terminate();
  worker = null;
}
