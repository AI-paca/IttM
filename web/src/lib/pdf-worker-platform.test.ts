import assert from "node:assert/strict";
import test from "node:test";
import {
  PdfWorkerCanvasFactory,
  PdfWorkerFilterFactory,
} from "./pdf-worker-platform";

class TestCanvas {
  width: number;
  height: number;
  readonly context = { kind: "2d" };
  contextOptions: unknown;

  constructor(width: number, height: number) {
    this.width = width;
    this.height = height;
  }

  getContext(_kind: string, options: unknown) {
    this.contextOptions = options;
    return this.context;
  }
}

test("PDF worker canvas factory uses and releases offscreen canvases", () => {
  const factory = new PdfWorkerCanvasFactory(
    {},
    TestCanvas as unknown as typeof OffscreenCanvas,
  );
  const entry = factory.create(120, 80);

  assert.equal(entry.canvas?.width, 120);
  assert.equal(entry.canvas?.height, 80);
  assert.deepEqual((entry.canvas as unknown as TestCanvas).contextOptions, {
    willReadFrequently: true,
  });

  factory.reset(entry, 60, 40);
  assert.equal(entry.canvas?.width, 60);
  assert.equal(entry.canvas?.height, 40);

  factory.destroy(entry);
  assert.equal(entry.canvas, null);
  assert.equal(entry.context, null);
});

test("PDF worker filter factory avoids DOM-backed SVG filters", () => {
  const factory = new PdfWorkerFilterFactory();

  assert.equal(factory.addFilter(), "none");
  assert.equal(factory.addHCMFilter(), "none");
  assert.equal(factory.addAlphaFilter(), "none");
  assert.equal(factory.addLuminosityFilter(), "none");
  assert.equal(factory.addHighlightHCMFilter(), "none");
});
