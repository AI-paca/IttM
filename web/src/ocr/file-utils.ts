import type { BrowserDiagnostics } from "./types";

export function getBrowserDiagnostics(): BrowserDiagnostics {
  const navigatorLike = globalThis.navigator as
    | (Navigator & { deviceMemory?: number; hardwareConcurrency?: number })
    | undefined;

  return {
    memory: navigatorLike?.deviceMemory ?? "Unknown",
    cores: navigatorLike?.hardwareConcurrency ?? "Unknown",
  };
}

export function isSupportedOcrFile(file: File): boolean {
  return file.type.startsWith("image/") || file.type === "application/pdf";
}

export function toBase64(file: File): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const base64 = result.split(",")[1];
      if (!base64) {
        reject(new Error("Не удалось прочитать файл как base64."));
        return;
      }
      resolve(base64);
    };
    reader.onerror = () =>
      reject(reader.error ?? new Error("Ошибка чтения файла."));
  });
}

export async function canvasToBase64(
  canvas: HTMLCanvasElement,
  mimeType = "image/jpeg",
  quality = 0.9,
): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          reject(new Error("Canvas вернул пустое изображение."));
          return;
        }
        const reader = new FileReader();
        reader.readAsDataURL(blob);
        reader.onloadend = () => {
          const result = typeof reader.result === "string" ? reader.result : "";
          const base64 = result.split(",")[1];
          if (!base64) reject(new Error("Не удалось сериализовать canvas."));
          else resolve(base64);
        };
        reader.onerror = () =>
          reject(reader.error ?? new Error("Ошибка чтения canvas."));
      },
      mimeType,
      quality,
    );
  });
}

export async function base64JpegToFile(
  base64: string,
  name = "page.jpg",
): Promise<File> {
  const response = await fetch(`data:image/jpeg;base64,${base64}`);
  const blob = await response.blob();
  return new File([blob], name, { type: "image/jpeg" });
}

export async function imageFileToCroppedBase64(
  file: File,
  cropWhiteBorders: (canvas: HTMLCanvasElement) => HTMLCanvasElement,
): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);

    img.onload = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = img.width;
        canvas.height = img.height;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        if (!ctx) {
          toBase64(file).then(resolve).catch(reject);
          return;
        }

        ctx.drawImage(img, 0, 0);
        const croppedCanvas = cropWhiteBorders(canvas);
        canvasToBase64(croppedCanvas).then(resolve).catch(reject);
      } catch (error) {
        reject(error);
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    };

    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("Не удалось загрузить изображение в браузере."));
    };

    img.src = objectUrl;
  });
}
