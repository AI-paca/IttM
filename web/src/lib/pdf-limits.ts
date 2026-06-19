export const MAX_BROWSER_PDF_BYTES = 128 * 1024 * 1024;

export function assertBrowserPdfSize(file: Pick<Blob, "size">): void {
  if (file.size > MAX_BROWSER_PDF_BYTES) {
    throw new Error(
      `PDF exceeds the ${MAX_BROWSER_PDF_BYTES} byte browser limit.`,
    );
  }
}
