const PDFJS_WASM_ROUTE = "vendor/pdfjs/wasm/";

export function pdfJsDocumentOptions(
  data: ArrayBuffer,
  baseUrl = import.meta.env.BASE_URL,
) {
  const normalizedBase = baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`;
  return {
    data,
    wasmUrl: `${normalizedBase}${PDFJS_WASM_ROUTE}`,
  };
}
