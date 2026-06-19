import { readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";
import {
  createCanvas,
  DOMMatrix,
  DOMPoint,
  ImageData,
  loadImage,
} from "@napi-rs/canvas";
import {
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "../web/src/ocr/browser-engine";
import {
  type BrowserOcrProfile,
  createBrowserOcrProfile,
} from "../web/src/ocr/browser-profile";
import { resolveBrowserBenchmarkProfile } from "./browser-benchmark-profile";

const globalRecord = globalThis as unknown as Record<string, unknown>;
globalRecord.document = {
  createElement(tagName: string) {
    if (tagName !== "canvas") {
      throw new Error(`Unsupported benchmark DOM element: ${tagName}`);
    }
    return createCanvas(1, 1);
  },
};
globalRecord.DOMMatrix = DOMMatrix;
globalRecord.DOMPoint = DOMPoint;
globalRecord.ImageData = ImageData;
globalRecord.createImageBitmap = async (blob: Blob) =>
  await loadImage(Buffer.from(await blob.arrayBuffer()));

function contentType(path: string): string {
  const extension = path.toLowerCase().split(".").pop();
  if (extension === "png") return "image/png";
  if (extension === "webp") return "image/webp";
  return "image/jpeg";
}

function snakeCase(value: string): string {
  return value.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

function browserProfileFlags(profile: BrowserOcrProfile): string[] {
  const flags = [
    "ocr_runtime:tesseract.js",
    `ocr_languages:${profile.languages}`,
    `ocr_max_image_pixels:${profile.maxImagePixels}`,
    `ocr_max_dimension:${profile.maxDimension}`,
    `ocr_border_pixels:${profile.ocrBorderPixels}`,
    `ocr_text_region_psm:${profile.textRegionPsm}`,
    `dense_grid_fallback:${profile.denseGridFallback}`,
    `dense_grid_target_width:${profile.denseGridTargetWidth}`,
    `edge_word_fallback_psm:${profile.edgeWordFallbackPsm}`,
    `edge_word_fallback_min_tokens:${profile.edgeWordFallbackMinTokens}`,
    `pdf_render_scale:${profile.pdfRenderScale}`,
    `browser_cache_worker:${profile.cacheWorker}`,
    `browser_profile_reason:${profile.reason}`,
    ...profile.imagePreprocessing.map((step) => `preprocess:${step}`),
    `layout_selector:${profile.layout.selector}`,
    ...profile.layout.allowedStages.map((stage) => `layout_stage:${stage}`),
  ];
  const preprocessRuntime = process.env.BROWSER_OCR_PREPROCESS_RUNTIME;
  flags.push(
    `preprocess_runtime:${
      preprocessRuntime ||
      (typeof document === "undefined" ? "none" : "browser_canvas")
    }`,
  );

  for (const [name, value] of Object.entries(
    profile.layout.defaultParameters,
  )) {
    flags.push(`layout_param:${snakeCase(name)}=${String(value)}`);
  }

  return flags.sort();
}

let profileName = process.env.BROWSER_OCR_PROFILE;
let source = "";
for (let index = 2; index < process.argv.length; index += 1) {
  const arg = process.argv[index];
  if (arg === "--profile") {
    profileName = process.argv[index + 1];
    index += 1;
    continue;
  }
  if (!source) {
    source = arg;
    continue;
  }
  console.error(`Unknown argument: ${arg}`);
  process.exit(2);
}
if (!source) {
  console.error("Usage: benchmark-browser-ocr.ts [--profile PROFILE] IMAGE");
  process.exit(2);
}

const bytes = await readFile(source);
const file = new File([bytes], basename(source), {
  type: contentType(source),
});
const langPath = resolve(
  process.env.BROWSER_OCR_LANG_PATH || ".cache/tessdata",
);
const pipelineProfile = resolveBrowserBenchmarkProfile(profileName);
const profile = {
  ...createBrowserOcrProfile(null, pipelineProfile),
  cacheWorker: false,
  langPath,
  cachePath: resolve(".cache/tesseract-js"),
  gzip: false,
};
const rssBefore = process.memoryUsage().rss;
const startedAt = performance.now();

try {
  const result = await runBrowserOcrLowMemory(
    file,
    () => undefined,
    undefined,
    profile,
  );
  if (!result.markdown.trim()) {
    throw new Error("Browser OCR produced empty Markdown.");
  }
  const elapsedMs = Math.round(performance.now() - startedAt);
  const rssAfter = process.memoryUsage().rss;
  process.stdout.write(
    JSON.stringify({
      markdown: result.markdown,
      elapsed_ms: elapsedMs,
      profile: profile.preprocessingProfile,
      flags: browserProfileFlags(profile).join("; "),
      rss_before_bytes: rssBefore,
      rss_after_bytes: rssAfter,
    }),
  );
} finally {
  await releaseBrowserOcrCache();
}
