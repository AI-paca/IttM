import assert from "node:assert/strict";
import process from "node:process";

const pageUrl = new URL(process.argv[2] || "http://127.0.0.1:4173/IttM/");
const expectedBase = normalizeBase(process.argv[3] || "/IttM/");
const retries = Number(process.env.PAGES_SMOKE_RETRIES || 12);
const retryDelayMs = Number(process.env.PAGES_SMOKE_DELAY_MS || 5_000);
const requiredAssets = [
  "vendor/tesseract/worker.min.js",
  "vendor/tesseract/tesseract-core-lstm.wasm.js",
  "vendor/tesseract/tesseract-core-simd-lstm.wasm.js",
  "vendor/tesseract/tesseract-core-relaxedsimd-lstm.wasm.js",
  "vendor/pdfjs/wasm/jbig2.wasm",
  "vendor/pdfjs/wasm/jbig2_nowasm_fallback.js",
  "vendor/pdfjs/wasm/openjpeg.wasm",
  "vendor/pdfjs/wasm/openjpeg_nowasm_fallback.js",
  "vendor/pdfjs/wasm/qcms_bg.wasm",
  "vendor/pdfjs/wasm/quickjs-eval.js",
  "vendor/pdfjs/wasm/quickjs-eval.wasm",
];

function normalizeBase(base) {
  const leading = base.startsWith("/") ? base : `/${base}`;
  return leading.endsWith("/") ? leading : `${leading}/`;
}

async function fetchWithRetry(url) {
  let lastError;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, {
        redirect: "follow",
        signal: AbortSignal.timeout(15_000),
      });
      if (response.ok) return response;
      lastError = new Error(`${url} returned HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    if (attempt < retries) {
      await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
    }
  }
  throw lastError;
}

assert.ok(
  pageUrl.pathname.startsWith(expectedBase),
  `${pageUrl.pathname} does not use expected base ${expectedBase}`,
);

const pageResponse = await fetchWithRetry(pageUrl);
assert.match(
  pageResponse.headers.get("content-type") || "",
  /text\/html/,
  "Pages root did not return HTML.",
);
const html = await pageResponse.text();
assert.ok(html.length > 0, "Pages root returned an empty body.");

const discovered = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
  .map((match) => match[1])
  .filter((value) => !/^(?:https?:|data:|#)/.test(value));
const assetUrls = new Set(
  [
    ...discovered.map((value) => new URL(value, pageUrl)),
    ...requiredAssets.map(
      (value) => new URL(`${expectedBase}${value}`, pageUrl),
    ),
  ].map((url) => url.href),
);

for (const assetUrl of assetUrls) {
  const url = new URL(assetUrl);
  assert.ok(
    url.pathname.startsWith(expectedBase),
    `${url.pathname} escaped expected base ${expectedBase}`,
  );
  const response = await fetchWithRetry(url);
  const bytes = await response.arrayBuffer();
  assert.ok(bytes.byteLength > 0, `${url.pathname} returned an empty body.`);
  if (/\.(?:js|mjs|css|wasm)$/i.test(url.pathname)) {
    assert.doesNotMatch(
      response.headers.get("content-type") || "",
      /text\/html/,
      `${url.pathname} returned an HTML fallback.`,
    );
  }
}

console.log(
  `Pages smoke passed for ${pageUrl.href}: ${assetUrls.size} assets loaded under ${expectedBase}`,
);
