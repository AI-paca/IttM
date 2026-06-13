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
