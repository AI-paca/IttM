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
  onProgress: (message: string) => void,
  preparePage: (pageNumber: number) => Promise<PreparedPdfPage>,
  processImage: (image: Blob) => Promise<string>,
  onChunkExtracted?: (text: string, pageIndex?: number) => void,
): Promise<string> {
  const markdownParts: string[] = [];

  for (let pageNumber = startPage; pageNumber <= totalPages; pageNumber += 1) {
    assertActive(options);
    onProgress(`Обработка страницы ${pageNumber} из ${totalPages}...`);
    const prepared = await preparePage(pageNumber);
    assertActive(options);
    onProgress(
      prepared.nativeText
        ? `Проверка изображения на странице ${pageNumber}...`
        : `Распознавание скана страницы ${pageNumber}...`,
    );
    const ocrText = await processImage(prepared.image);
    assertActive(options);
    const pageText = mergeNativeAndOcrText(prepared.nativeText, ocrText);

    if (pageText.trim()) {
      markdownParts.push(pageText);
      onChunkExtracted?.(`${pageText}\n\n---\n\n`, pageNumber);
    }
  }

  return markdownParts.join("\n\n---\n\n");
}
