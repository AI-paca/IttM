import { mkdir, writeFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import {
  createCanvas,
  DOMMatrix,
  DOMPoint,
  ImageData,
  loadImage,
} from "@napi-rs/canvas";
import { streamImagesForBrowserOcr } from "../../web/src/ocr/browser-image-preprocessor";
import { createBrowserOcrProfile } from "../../web/src/ocr/browser-profile";
import { resolveBrowserBenchmarkProfile } from "../benchmark/browser-benchmark-profile";

const globalRecord = globalThis as unknown as Record<string, unknown>;
globalRecord.document = {
  createElement(tagName: string) {
    if (tagName !== "canvas") {
      throw new Error(`Unsupported debug DOM element: ${tagName}`);
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

let profileName = process.env.BROWSER_OCR_PROFILE;
let outputRoot = "";
const sources: string[] = [];
for (let index = 2; index < process.argv.length; index += 1) {
  const arg = process.argv[index];
  if (arg === "--profile") {
    profileName = process.argv[index + 1];
    index += 1;
    continue;
  }
  if (arg === "--output") {
    outputRoot = process.argv[index + 1];
    index += 1;
    continue;
  }
  sources.push(arg);
}

if (!outputRoot || sources.length === 0) {
  console.error(
    "Usage: dump-browser-ocr-inputs.ts --output DIR [--profile PROFILE] IMAGE...",
  );
  process.exit(2);
}

const pipelineProfile = resolveBrowserBenchmarkProfile(profileName);
const profile = {
  ...createBrowserOcrProfile(null, pipelineProfile),
  cacheWorker: false,
  langPath: resolve(process.env.BROWSER_OCR_LANG_PATH || ".cache/tessdata"),
  cachePath: resolve(".cache/tesseract-js"),
  gzip: false,
};

const artifactRoot = resolve(outputRoot);
await mkdir(artifactRoot, { recursive: true });
const manifest: string[] = [
  "# Browser OCR Prepared Inputs",
  "",
  `- profile: \`${profile.preprocessingProfile}\``,
  `- languages: \`${profile.languages}\``,
  `- max_dimension: \`${profile.maxDimension}\``,
  `- dense_grid_fallback: \`${profile.denseGridFallback}\``,
  `- table_slot_builder: \`${profile.tableSlotBuilder}\``,
  `- table_slot_max_columns: \`${profile.tableSlotMaxColumns}\``,
  `- layout_selector: \`${profile.layout.selector}\``,
  `- layout_stages: \`${profile.layout.allowedStages.join(",")}\``,
  "",
];
const html: string[] = [
  "<!doctype html>",
  '<html lang="en">',
  "<head>",
  '<meta charset="utf-8" />',
  "<title>Browser OCR Prepared Inputs</title>",
  "<style>",
  "body{font-family:system-ui,sans-serif;margin:16px;background:#f6f6f6;color:#111}",
  "section{margin:0 0 28px}",
  ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}",
  "figure{margin:0;padding:8px;background:white;border:1px solid #ccc}",
  "img{max-width:100%;height:auto;display:block;border:1px solid #ddd}",
  "figcaption{font-size:12px;line-height:1.35;margin-top:6px;white-space:pre-wrap}",
  "</style>",
  "</head>",
  "<body>",
  "<h1>Browser OCR Prepared Inputs</h1>",
];

for (const source of sources) {
  const bytes = await import("node:fs/promises").then(({ readFile }) =>
    readFile(source),
  );
  const file = new File([bytes], basename(source), {
    type: contentType(source),
  });
  const safeName = basename(source).replace(/[^A-Za-z0-9_.-]+/g, "_");
  const fileDir = join(artifactRoot, safeName);
  await mkdir(fileDir, { recursive: true });

  manifest.push(`## ${basename(source)}`, "");
  html.push(`<section><h2>${basename(source)}</h2><div class="grid">`);

  let count = 0;
  for await (const prepared of streamImagesForBrowserOcr(file, profile)) {
    const extension = prepared.input.type.includes("png") ? "png" : "jpg";
    const name = `input-${String(prepared.index + 1).padStart(3, "0")}-of-${String(
      prepared.total,
    ).padStart(
      3,
      "0",
    )}-psm-${prepared.pageSegmentationMode || "profile"}.${extension}`;
    const buffer = Buffer.from(await prepared.input.arrayBuffer());
    await writeFile(join(fileDir, name), buffer);
    count += 1;
    manifest.push(
      `- \`${safeName}/${name}\`: index=${prepared.index + 1}/${prepared.total}, psm=${
        prepared.pageSegmentationMode || "profile"
      }, bytes=${buffer.length}`,
    );
    html.push(
      `<figure><img src="${safeName}/${name}" /><figcaption>${name}\nindex ${
        prepared.index + 1
      }/${prepared.total}; psm=${prepared.pageSegmentationMode || "profile"}</figcaption></figure>`,
    );
  }

  manifest.push(`- total_inputs: \`${count}\``, "");
  html.push("</div></section>");
}

html.push("</body></html>");
await writeFile(join(artifactRoot, "manifest.md"), `${manifest.join("\n")}\n`);
await writeFile(join(artifactRoot, "index.html"), `${html.join("\n")}\n`);
console.log(`Wrote browser OCR prepared inputs to ${artifactRoot}`);
