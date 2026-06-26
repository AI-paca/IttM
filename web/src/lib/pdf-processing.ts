import { mergeNativeAndOcrText } from "../ocr/pdf-text";

export interface PdfProcessingOptions {
  renderScale?: number;
  maxPagePixels?: number;
  maxDimension?: number;
  cropMode?: "auto" | "none";
  shouldContinue?: () => boolean;
}

export interface PreparedPdfPage {
  nativeText: string;
  image: Blob;
}

export interface PdfProgressDetail {
  currentPage: number;
  totalPages: number;
  completedPages?: number;
  currentPagePercent?: number | null;
}

function assertActive(options: PdfProcessingOptions) {
  if (options.shouldContinue && !options.shouldContinue()) {
    throw new DOMException("PDF processing was cancelled.", "AbortError");
  }
}

export function boundedViewportScale(
  width: number,
  height: number,
  options: PdfProcessingOptions,
): number {
  const maxDimension = options.maxDimension ?? 4096;
  const maxPagePixels = options.maxPagePixels ?? 12_000_000;
  const dimensionScale = Math.min(1, maxDimension / Math.max(width, height));
  const pixelScale = Math.min(
    1,
    Math.sqrt(maxPagePixels / Math.max(width * height, 1)),
  );
  return Math.min(dimensionScale, pixelScale);
}

export async function processPreparedPages(
  totalPages: number,
  startPage: number,
  options: PdfProcessingOptions,
  onProgress: (message: string, detail?: PdfProgressDetail) => void,
  preparePage: (pageNumber: number) => Promise<PreparedPdfPage>,
  processImage: (
    image: Blob,
    pageNumber: number,
    totalPages: number,
  ) => Promise<string>,
  onChunkExtracted?: (text: string, pageIndex?: number) => void,
): Promise<string> {
  const markdownParts: string[] = [];

  for (let pageNumber = startPage; pageNumber <= totalPages; pageNumber += 1) {
    assertActive(options);
    onProgress(`Обработка страницы ${pageNumber} из ${totalPages}...`, {
      currentPage: pageNumber,
      totalPages,
      completedPages: pageNumber - 1,
      currentPagePercent: 0.05,
    });
    const prepared = await preparePage(pageNumber);
    assertActive(options);
    onProgress(
      prepared.nativeText
        ? `Проверка изображения на странице ${pageNumber}...`
        : `Распознавание скана страницы ${pageNumber}...`,
      {
        currentPage: pageNumber,
        totalPages,
        completedPages: pageNumber - 1,
        currentPagePercent: 0.25,
      },
    );
    const ocrText = await processImage(prepared.image, pageNumber, totalPages);
    assertActive(options);
    const pageText = mergeNativeAndOcrText(prepared.nativeText, ocrText);

    if (pageText.trim()) {
      markdownParts.push(pageText);
      onChunkExtracted?.(`${pageText}\n\n---\n\n`, pageNumber);
    }
    onProgress(`Страница ${pageNumber} обработана.`, {
      currentPage: pageNumber,
      totalPages,
      completedPages: pageNumber,
      currentPagePercent: null,
    });
  }

  return markdownParts.join("\n\n---\n\n");
}
