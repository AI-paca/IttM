import * as pdfjsLib from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { findContentBounds } from "./image-content-bounds";
import { assertBrowserPdfSize } from "./pdf-limits";
import { boundedViewportScale, processPreparedPages } from "./pdf-processing";
import type {
  PdfProcessingOptions,
  PdfProgressDetail,
  PreparedPdfPage,
} from "./pdf-processing";
import { pdfJsDocumentOptions } from "./pdfjs-options";

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export type { PdfProcessingOptions } from "./pdf-processing";

interface PdfTextItem {
  str?: string;
  hasEOL?: boolean;
}

interface PdfWorkerResponse<T> {
  id: number;
  ok: boolean;
  value?: T;
  error?: string;
}

let nextPdfWorkerRequestId = 0;
const PDF_WORKER_REQUEST_TIMEOUT_MS = 120_000;

class PdfPageWorkerClient {
  private readonly worker = new Worker(
    new URL("./pdf-page.worker.ts", import.meta.url),
    { type: "module" },
  );
  private readonly pending = new Map<
    number,
    {
      resolve: (value: unknown) => void;
      reject: (error: Error) => void;
      timeout: ReturnType<typeof setTimeout>;
      cancellationCheck?: ReturnType<typeof setInterval>;
    }
  >();
  private failure: Error | null = null;

  constructor() {
    this.worker.onmessage = (
      event: MessageEvent<PdfWorkerResponse<unknown>>,
    ) => {
      const request = this.pending.get(event.data.id);
      if (!request) return;
      this.pending.delete(event.data.id);
      clearTimeout(request.timeout);
      if (request.cancellationCheck) {
        clearInterval(request.cancellationCheck);
      }
      if (event.data.ok) request.resolve(event.data.value);
      else request.reject(new Error(event.data.error || "PDF worker failed."));
    };
    this.worker.onerror = (event) => {
      this.fail(new Error(event.message || "PDF worker failed."));
    };
  }

  private fail(error: Error) {
    if (this.failure) return;
    this.failure = error;
    for (const request of this.pending.values()) {
      clearTimeout(request.timeout);
      if (request.cancellationCheck) {
        clearInterval(request.cancellationCheck);
      }
      request.reject(error);
    }
    this.pending.clear();
    this.worker.terminate();
  }

  request<T>(
    payload: Record<string, unknown>,
    shouldContinue?: () => boolean,
  ): Promise<T> {
    if (this.failure) return Promise.reject(this.failure);
    if (shouldContinue && !shouldContinue()) {
      return Promise.reject(
        new DOMException("PDF processing was cancelled.", "AbortError"),
      );
    }

    return new Promise<T>((resolve, reject) => {
      const id = ++nextPdfWorkerRequestId;
      const timeout = setTimeout(() => {
        this.fail(new Error("PDF worker request timed out."));
      }, PDF_WORKER_REQUEST_TIMEOUT_MS);
      const cancellationCheck = shouldContinue
        ? setInterval(() => {
            if (!shouldContinue()) {
              this.fail(
                new DOMException("PDF processing was cancelled.", "AbortError"),
              );
            }
          }, 100)
        : undefined;
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timeout,
        cancellationCheck,
      });
      try {
        this.worker.postMessage({ id, ...payload });
      } catch (error) {
        this.fail(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  async dispose() {
    if (this.failure) return;
    try {
      await this.request({ action: "dispose" });
    } finally {
      this.worker.terminate();
    }
  }
}

function normalizedPdfText(items: PdfTextItem[]): string {
  return items
    .map((item) => `${item.str ?? ""}${item.hasEOL ? "\n" : " "}`)
    .join("")
    .replace(/[ \t]+/g, " ")
    .replace(/\n[ \t]+/g, "\n")
    .trim();
}

async function processPdfInWorker(
  file: File,
  onProgress: (msg: string, detail?: PdfProgressDetail) => void,
  processImageCallback: (
    image: Blob,
    pageNumber: number,
    totalPages: number,
  ) => Promise<string>,
  onChunkExtracted: ((text: string, pageIdx?: number) => void) | undefined,
  startPage: number,
  onTotalPages: ((total: number) => void) | undefined,
  options: PdfProcessingOptions,
): Promise<string> {
  const client = new PdfPageWorkerClient();
  try {
    const { totalPages } = await client.request<{ totalPages: number }>(
      {
        action: "init",
        file,
        renderScale: options.renderScale ?? 1.5,
        maxPagePixels: options.maxPagePixels ?? 12_000_000,
        maxDimension: options.maxDimension ?? 4096,
        cropMode: options.cropMode ?? "auto",
      },
      options.shouldContinue,
    );
    onTotalPages?.(totalPages);
    return await processPreparedPages(
      totalPages,
      startPage,
      options,
      onProgress,
      (pageNumber) =>
        client.request<PreparedPdfPage>(
          { action: "page", pageNumber },
          options.shouldContinue,
        ),
      processImageCallback,
      onChunkExtracted,
    );
  } finally {
    await client.dispose();
  }
}

async function processPdfOnMainThread(
  file: File,
  onProgress: (msg: string, detail?: PdfProgressDetail) => void,
  processImageCallback: (
    image: Blob,
    pageNumber: number,
    totalPages: number,
  ) => Promise<string>,
  onChunkExtracted: ((text: string, pageIdx?: number) => void) | undefined,
  startPage: number,
  onTotalPages: ((total: number) => void) | undefined,
  options: PdfProcessingOptions,
): Promise<string> {
  const loadingTask = pdfjsLib.getDocument(
    pdfJsDocumentOptions(await file.arrayBuffer()),
  );
  const pdf = await loadingTask.promise;
  onTotalPages?.(pdf.numPages);

  try {
    return await processPreparedPages(
      pdf.numPages,
      startPage,
      options,
      onProgress,
      async (pageNumber) => {
        const page = await pdf.getPage(pageNumber);
        try {
          const textContent = await page.getTextContent();
          const renderScale = options.renderScale ?? 1.5;
          const requestedViewport = page.getViewport({ scale: renderScale });
          const viewport = page.getViewport({
            scale:
              renderScale *
              boundedViewportScale(
                requestedViewport.width,
                requestedViewport.height,
                options,
              ),
          });
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.ceil(viewport.width));
          canvas.height = Math.max(1, Math.ceil(viewport.height));
          const context = canvas.getContext("2d", {
            willReadFrequently: true,
          });
          if (!context) throw new Error("Could not create PDF canvas.");
          await page.render({ canvasContext: context, viewport } as never)
            .promise;
          const cropped =
            options.cropMode === "none" ? canvas : cropWhiteBorders(canvas);
          const image = await new Promise<Blob>((resolve, reject) => {
            cropped.toBlob(
              (blob) =>
                blob
                  ? resolve(blob)
                  : reject(new Error("Canvas returned no PDF page.")),
              "image/jpeg",
              0.9,
            );
          });
          canvas.width = 1;
          canvas.height = 1;
          return {
            nativeText: normalizedPdfText(textContent.items as PdfTextItem[]),
            image,
          };
        } finally {
          page.cleanup();
        }
      },
      processImageCallback,
      onChunkExtracted,
    );
  } finally {
    await pdf.cleanup();
    await loadingTask.destroy();
  }
}

export async function processPdfIntelligently(
  file: File,
  onProgress: (msg: string, detail?: PdfProgressDetail) => void,
  processImageCallback: (
    image: Blob,
    pageNumber: number,
    totalPages: number,
  ) => Promise<string>,
  onChunkExtracted?: (text: string, pageIdx?: number) => void,
  startPage = 1,
  onTotalPages?: (total: number) => void,
  options: PdfProcessingOptions = {},
): Promise<string> {
  assertBrowserPdfSize(file);
  onProgress("Загрузка PDF...");
  if (typeof Worker !== "undefined" && typeof OffscreenCanvas !== "undefined") {
    return await processPdfInWorker(
      file,
      onProgress,
      processImageCallback,
      onChunkExtracted,
      startPage,
      onTotalPages,
      options,
    );
  }
  return await processPdfOnMainThread(
    file,
    onProgress,
    processImageCallback,
    onChunkExtracted,
    startPage,
    onTotalPages,
    options,
  );
}

export function cropWhiteBorders(canvas: HTMLCanvasElement): HTMLCanvasElement {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return canvas;
  const imageData = context.getImageData(0, 0, canvas.width, canvas.height);
  const bounds = findContentBounds(imageData.data, canvas.width, canvas.height);
  const cropWidth = Math.max(1, bounds.right - bounds.left);
  const cropHeight = Math.max(1, bounds.bottom - bounds.top);
  if (cropWidth === canvas.width && cropHeight === canvas.height) return canvas;

  const cropped = document.createElement("canvas");
  cropped.width = cropWidth;
  cropped.height = cropHeight;
  cropped
    .getContext("2d")
    ?.drawImage(
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
  return cropped;
}
