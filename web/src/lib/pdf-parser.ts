import * as pdfjsLib from "pdfjs-dist";
// Configure worker
pdfjsLib.GlobalWorkerOptions.workerSrc = `https://unpkg.com/pdfjs-dist@${pdfjsLib.version}/build/pdf.worker.min.mjs`;

export async function processPdfIntelligently(
  file: File,
  onProgress: (msg: string) => void,
  processImageCallback: (b64Image: string) => Promise<string>,
  onChunkExtracted?: (text: string, pageIdx?: number) => void,
  startPage: number = 1,
  onTotalPages?: (total: number) => void,
): Promise<string> {
  onProgress("Загрузка PDF...");
  const arrayBuffer = await file.arrayBuffer();
  const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
  const numPages = pdf.numPages;
  if (onTotalPages) onTotalPages(numPages);
  const markdownParts: string[] = [];

  for (let i = startPage; i <= numPages; i++) {
    onProgress(`Обработка страницы ${i} из ${numPages}...`);
    const page = await pdf.getPage(i);

    // 1. Try to extract native text
    const textContent = await page.getTextContent();
    let nativeText = "";

    for (const item of textContent.items as any[]) {
      nativeText += item.str + (item.hasEOL ? "\n" : " ");
    }
    // Clean up multiple spaces but keep newlines
    nativeText = nativeText
      .replace(/[ \t]+/g, " ")
      .replace(/\n[ \t]+/g, "\n")
      .trim();

    // 2. Render to canvas to get image base64, and crop empty regions
    const viewport = page.getViewport({ scale: 1.5 }); // Good resolution
    const canvas = document.createElement("canvas");
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    if (ctx) {
      await page.render({ canvasContext: ctx, viewport } as any).promise;

      // Smart crop: remove white borders
      const croppedCanvas = cropWhiteBorders(canvas);

      // If native text is long enough, we trust it over OCR.
      if (nativeText.length > 50) {
        markdownParts.push(nativeText);
        if (onChunkExtracted) onChunkExtracted(nativeText + "\n\n---\n\n", i);
      } else {
        // Scanned PDF, needs OCR
        onProgress(`Распознавание скана страницы ${i}...`);
        const b64 = await new Promise<string>((resolve, reject) => {
          croppedCanvas.toBlob(
            (blob) => {
              if (!blob) return reject("Canvas is empty");
              const reader = new FileReader();
              reader.readAsDataURL(blob);
              reader.onloadend = () => {
                const res = reader.result as string;
                resolve(res.split(",")[1]);
              };
              reader.onerror = reject;
            },
            "image/jpeg",
            0.9,
          );
        });

        const ocrText = await processImageCallback(b64);
        if (ocrText.trim()) {
          markdownParts.push(ocrText);
          if (onChunkExtracted) onChunkExtracted(ocrText + "\n\n---\n\n", i);
        }
      }
    } else {
      // fallback if canvas fails
      if (nativeText.trim()) {
        markdownParts.push(nativeText);
        if (onChunkExtracted) onChunkExtracted(nativeText + "\n\n---\n\n", i);
      }
    }
  }

  return markdownParts.join("\n\n---\n\n");
}

// Function to find bounding box of non-white pixels and crop canvas
export function cropWhiteBorders(canvas: HTMLCanvasElement): HTMLCanvasElement {
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return canvas;

  const width = canvas.width;
  const height = canvas.height;
  const imgData = ctx.getImageData(0, 0, width, height);
  const data = imgData.data;

  let top = 0,
    bottom = height,
    left = 0,
    right = width;
  const threshold = 240; // 255 is white, allow slight variations

  // Find top
  topLoop: for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * 4;
      if (data[idx] < threshold || data[idx + 1] < threshold || data[idx + 2] < threshold) {
        top = y;
        break topLoop;
      }
    }
  }

  // Find bottom
  bottomLoop: for (let y = height - 1; y >= 0; y--) {
    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * 4;
      if (data[idx] < threshold || data[idx + 1] < threshold || data[idx + 2] < threshold) {
        bottom = y;
        break bottomLoop;
      }
    }
  }

  // Find left
  leftLoop: for (let x = 0; x < width; x++) {
    for (let y = top; y <= bottom; y++) {
      const idx = (y * width + x) * 4;
      if (data[idx] < threshold || data[idx + 1] < threshold || data[idx + 2] < threshold) {
        left = x;
        break leftLoop;
      }
    }
  }

  // Find right
  rightLoop: for (let x = width - 1; x >= 0; x--) {
    for (let y = top; y <= bottom; y++) {
      const idx = (y * width + x) * 4;
      if (data[idx] < threshold || data[idx + 1] < threshold || data[idx + 2] < threshold) {
        right = x;
        break rightLoop;
      }
    }
  }

  // Add padding
  const padding = 20;
  left = Math.max(0, left - padding);
  top = Math.max(0, top - padding);
  right = Math.min(width, right + padding);
  bottom = Math.min(height, bottom + padding);

  const cropWidth = right - left;
  const cropHeight = bottom - top;

  if (cropWidth <= 0 || cropHeight <= 0) return canvas;

  const croppedCanvas = document.createElement("canvas");
  croppedCanvas.width = cropWidth;
  croppedCanvas.height = cropHeight;
  const croppedCtx = croppedCanvas.getContext("2d");
  if (croppedCtx) {
    croppedCtx.drawImage(canvas, left, top, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);
    return croppedCanvas;
  }

  return canvas;
}
