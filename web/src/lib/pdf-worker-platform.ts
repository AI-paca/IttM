interface CanvasFactoryOptions {
  enableHWA?: boolean;
}

interface WorkerCanvasEntry {
  canvas: OffscreenCanvas | null;
  context: OffscreenCanvasRenderingContext2D | null;
}

type OffscreenCanvasConstructor = new (
  width: number,
  height: number,
) => OffscreenCanvas;

export class PdfWorkerCanvasFactory {
  private readonly Canvas: OffscreenCanvasConstructor;
  private readonly willReadFrequently: boolean;

  constructor(
    options: CanvasFactoryOptions = {},
    Canvas: OffscreenCanvasConstructor = OffscreenCanvas,
  ) {
    this.Canvas = Canvas;
    this.willReadFrequently = !options.enableHWA;
  }

  create(width: number, height: number): WorkerCanvasEntry {
    if (width <= 0 || height <= 0) {
      throw new Error("Invalid canvas size");
    }
    const canvas = new this.Canvas(width, height);
    return {
      canvas,
      context: canvas.getContext("2d", {
        willReadFrequently: this.willReadFrequently,
      }),
    };
  }

  reset(entry: WorkerCanvasEntry, width: number, height: number) {
    if (!entry.canvas) throw new Error("Canvas is not specified");
    if (width <= 0 || height <= 0) {
      throw new Error("Invalid canvas size");
    }
    entry.canvas.width = width;
    entry.canvas.height = height;
  }

  destroy(entry: WorkerCanvasEntry) {
    if (!entry.canvas) throw new Error("Canvas is not specified");
    entry.canvas.width = 0;
    entry.canvas.height = 0;
    entry.canvas = null;
    entry.context = null;
  }
}

export class PdfWorkerFilterFactory {
  addFilter() {
    return "none";
  }

  addHCMFilter() {
    return "none";
  }

  addAlphaFilter() {
    return "none";
  }

  addLuminosityFilter() {
    return "none";
  }

  addHighlightHCMFilter() {
    return "none";
  }

  destroy() {}
}
