import { createWorker } from "tesseract.js";

export async function runBrowserOcrLowMemory(
  file: File,
  onProgress: (msg: string, percent?: number) => void,
  onChunkExtracted?: (text: string) => void,
): Promise<{ markdown: string }> {
  onProgress("Загрузка движка распознавания...");

  const worker = await createWorker("rus+eng", 1, {
    logger: (m) => {
      if (m.status === "recognizing text" && m.progress !== undefined) {
        onProgress(`Распознавание... ${Math.round(m.progress * 100)}%`, m.progress);
      } else {
        onProgress(m.status);
      }
    },
  });

  onProgress("Обработка изображения...");
  const {
    data: { text },
  } = await worker.recognize(file);

  if (onChunkExtracted) {
    onChunkExtracted(text);
  }

  onProgress("Зачистка памяти...");
  await worker.terminate();

  return { markdown: text };
}
