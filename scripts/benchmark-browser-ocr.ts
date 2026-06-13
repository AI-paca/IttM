import { readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";
import {
  releaseBrowserOcrCache,
  runBrowserOcrLowMemory,
} from "../web/src/ocr/browser-engine";
import { createBrowserOcrProfile } from "../web/src/ocr/browser-profile";

function contentType(path: string): string {
  const extension = path.toLowerCase().split(".").pop();
  if (extension === "png") return "image/png";
  if (extension === "webp") return "image/webp";
  return "image/jpeg";
}

const source = process.argv[2];
if (!source) {
  console.error("Usage: benchmark-browser-ocr.ts IMAGE");
  process.exit(2);
}

const bytes = await readFile(source);
const file = new File([bytes], basename(source), {
  type: contentType(source),
});
const profile = {
  ...createBrowserOcrProfile(null),
  cacheWorker: false,
  langPath: resolve("."),
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
  const elapsedMs = Math.round(performance.now() - startedAt);
  const rssAfter = process.memoryUsage().rss;
  process.stdout.write(
    JSON.stringify({
      markdown: result.markdown,
      elapsed_ms: elapsedMs,
      rss_before_bytes: rssBefore,
      rss_after_bytes: rssAfter,
    }),
  );
} finally {
  await releaseBrowserOcrCache();
}
