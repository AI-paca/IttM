import * as pdfjsLib from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { findContentBounds } from "./image-content-bounds";
import {
  PdfWorkerCanvasFactory,
  PdfWorkerFilterFactory,
} from "./pdf-worker-platform";
import { pdfJsDocumentOptions } from "./pdfjs-options";

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

interface InitRequest {
  id: number;
  action: "init";
  file: File;
  renderScale: number;
  maxPagePixels: number;
  maxDimension: number;
}

interface PageRequest {
  id: number;
  action: "page";
  pageNumber: number;
}

interface DisposeRequest {
  id: number;
  action: "dispose";
}

type PdfWorkerRequest = InitRequest | PageRequest | DisposeRequest;

interface PdfTextItem {
  str?: string;
  hasEOL?: boolean;
}

let documentTask: ReturnType<typeof pdfjsLib.getDocument> | null = null;
let pdfDocument: Awaited<
  ReturnType<typeof pdfjsLib.getDocument>["promise"]
> | null = null;
let settings = {
  renderScale: 1.5,
  maxPagePixels: 12_000_000,
  maxDimension: 4096,
};

function normalizedText(items: PdfTextItem[]): string {
  return items
    .map((item) => `${item.str ?? ""}${item.hasEOL ? "\n" : " "}`)
    .join("")
    .replace(/[ \t]+/g, " ")
    .replace(/\n[ \t]+/g, "\n")
    .trim();
}

function boundedViewport(
  page: Awaited<ReturnType<NonNullable<typeof pdfDocument>["getPage"]>>,
) {
  const requested = page.getViewport({ scale: settings.renderScale });
  const dimensionScale = Math.min(
    1,
    settings.maxDimension / Math.max(requested.width, requested.height),
  );
  const pixelScale = Math.min(
    1,
    Math.sqrt(
      settings.maxPagePixels / Math.max(requested.width * requested.height, 1),
    ),
  );
  return page.getViewport({
    scale: settings.renderScale * Math.min(dimensionScale, pixelScale),
  });
}

async function renderPage(pageNumber: number) {
  if (!pdfDocument) throw new Error("PDF worker is not initialized.");
  const page = await pdfDocument.getPage(pageNumber);
  try {
    const [textContent, viewport] = await Promise.all([
      page.getTextContent(),
      Promise.resolve(boundedViewport(page)),
    ]);
    const canvas = new OffscreenCanvas(
      Math.max(1, Math.ceil(viewport.width)),
      Math.max(1, Math.ceil(viewport.height)),
    );
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("Could not create PDF OffscreenCanvas.");
    await page.render({
      canvas: canvas as unknown as HTMLCanvasElement,
      canvasContext: context as unknown as CanvasRenderingContext2D,
      viewport,
    }).promise;

    const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
    const bounds = findContentBounds(
      imageData.data,
      canvas.width,
      canvas.height,
    );
    const cropWidth = Math.max(1, bounds.right - bounds.left);
    const cropHeight = Math.max(1, bounds.bottom - bounds.top);
    const output = new OffscreenCanvas(cropWidth, cropHeight);
    const outputContext = output.getContext("2d");
    if (!outputContext) throw new Error("Could not create PDF output canvas.");
    outputContext.drawImage(
      canvas,
      bounds.left,
      bounds.top,
      cropWidth,
      cropHeight,
      0,
      0,
      cropWidth,
      cropHeight,
    );
    canvas.width = 1;
    canvas.height = 1;

    return {
      nativeText: normalizedText(textContent.items as PdfTextItem[]),
      image: await output.convertToBlob({ type: "image/jpeg", quality: 0.9 }),
    };
  } finally {
    page.cleanup();
  }
}

async function dispose() {
  await pdfDocument?.cleanup();
  await documentTask?.destroy();
  documentTask = null;
  pdfDocument = null;
}

self.onmessage = async (event: MessageEvent<PdfWorkerRequest>) => {
  const request = event.data;
  try {
    if (request.action === "init") {
      await dispose();
      settings = {
        renderScale: request.renderScale,
        maxPagePixels: request.maxPagePixels,
        maxDimension: request.maxDimension,
      };
      documentTask = pdfjsLib.getDocument({
        ...pdfJsDocumentOptions(await request.file.arrayBuffer()),
        CanvasFactory: PdfWorkerCanvasFactory as never,
        FilterFactory: PdfWorkerFilterFactory as never,
        disableFontFace: true,
        useSystemFonts: false,
        isOffscreenCanvasSupported: true,
      });
      pdfDocument = await documentTask.promise;
      self.postMessage({
        id: request.id,
        ok: true,
        value: { totalPages: pdfDocument.numPages },
      });
      return;
    }

    if (request.action === "page") {
      self.postMessage({
        id: request.id,
        ok: true,
        value: await renderPage(request.pageNumber),
      });
      return;
    }

    await dispose();
    self.postMessage({ id: request.id, ok: true, value: null });
  } catch (error) {
    self.postMessage({
      id: request.id,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};

export {};
