import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { basename, join, resolve } from "node:path";
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
} from "../../web/src/ocr/browser-engine";
import {
  type BrowserOcrProfile,
  createBrowserOcrProfile,
} from "../../web/src/ocr/browser-profile";
import { resolveBrowserBenchmarkProfile } from "./browser-benchmark-profile";
import { configureBrowserPipelineCoreUrl } from "../../web/src/ocr/pipeline-core";

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

function browserProfileFlags(profile: BrowserOcrProfile): string[] {
  const flags = [
    "pipeline:rust_separated_v1",
    "ocr_runtime:tesseract.js",
    `ocr_languages:${profile.languages}`,
    `ocr_text_region_psm:${profile.textRegionPsm}`,
    `browser_cache_worker:${profile.cacheWorker}`,
    `browser_profile_reason:${profile.reason}`,
  ];
  const preprocessRuntime = process.env.BROWSER_OCR_PREPROCESS_RUNTIME;
  flags.push(
    `preprocess_runtime:${
      preprocessRuntime ||
      (typeof document === "undefined" ? "none" : "browser_canvas")
    }`,
  );

  return flags.sort();
}

let profileName = process.env.BROWSER_OCR_PROFILE;
let source = "";
let artifactRoot = "";
for (let index = 2; index < process.argv.length; index += 1) {
  const arg = process.argv[index];
  if (arg === "--profile") {
    profileName = process.argv[index + 1];
    index += 1;
    continue;
  }
  if (arg === "--artifacts") {
    artifactRoot = resolve(process.argv[index + 1]);
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
  console.error(
    "Usage: benchmark-browser-ocr.ts [--profile PROFILE] [--artifacts DIR] IMAGE",
  );
  process.exit(2);
}

const bytes = await readFile(source);
const pipelineCoreBytes = await readFile(
  resolve("web/public/wasm/ittm_pipeline_core.wasm"),
);
configureBrowserPipelineCoreUrl(
  `data:application/wasm;base64,${pipelineCoreBytes.toString("base64")}`,
);
const file = new File([bytes], basename(source), {
  type: contentType(source),
});
const langPath = resolve(
  process.env.BROWSER_OCR_LANG_PATH || ".cache/tessdata",
);
async function availableLanguages(path: string): Promise<string[] | undefined> {
  try {
    const files = await readdir(path);
    return files
      .filter((file) => file.endsWith(".traineddata"))
      .map((file) => file.slice(0, -".traineddata".length))
      .sort();
  } catch {
    return undefined;
  }
}

const pipelineProfile = resolveBrowserBenchmarkProfile(profileName);
const profile = {
  ...createBrowserOcrProfile(null, pipelineProfile),
  cacheWorker: false,
  langPath,
  cachePath: resolve(".cache/tesseract-js"),
  gzip: false,
  availableLanguages: await availableLanguages(langPath),
};
const rssBefore = process.memoryUsage().rss;
const startedAt = performance.now();
const debugJobs: Array<{
  index: number;
  bbox: readonly [number, number, number, number];
  objectId: number;
  row: number;
  column: number;
  rowSpan: number;
  columnSpan: number;
  recognitionMode: number;
  objectKind: number;
  text?: string;
  confidenceMilli?: number;
}> = [];
let debugRouteId = 0;

async function writeJson(path: string, value: unknown) {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

async function prepareArtifactRoot() {
  if (!artifactRoot) return;
  for (const directory of [
    "00-preprocess",
    "01-geometry",
    "02-topology",
    "03-find-object",
    "04-separate-block",
    "05-ocr-blocks",
    "06-get-segment",
    "07-generate-object",
  ]) {
    await mkdir(join(artifactRoot, directory), { recursive: true });
  }
  const sourceImage = await loadImage(bytes);
  const canvas = createCanvas(sourceImage.width, sourceImage.height);
  canvas.getContext("2d").drawImage(sourceImage, 0, 0);
  await writeFile(
    join(artifactRoot, "00-preprocess", "raster.png"),
    canvas.toBuffer("image/png"),
  );
  await writeJson(join(artifactRoot, "01-geometry", "manifest.json"), {
    stage: "geometry",
    status: "opaque",
    issue:
      "Rust core marks this stage complete but ABI v6 does not export its geometry state.",
  });
  await writeJson(join(artifactRoot, "02-topology", "manifest.json"), {
    stage: "topology",
    status: "opaque",
    issue:
      "Rust core marks this stage complete but ABI v6 does not export its topology state.",
  });
}

async function finishArtifacts(markdown: string) {
  if (!artifactRoot) return;
  await writeJson(join(artifactRoot, "04-separate-block", "manifest.json"), {
    stage: "separate-block",
    route_id: debugRouteId,
    jobs: debugJobs,
  });
  const sourceImage = await loadImage(bytes);
  const objectIds = [...new Set(debugJobs.map((job) => job.objectId))].sort(
    (a, b) => a - b,
  );
  for (const objectId of objectIds) {
    const jobs = debugJobs.filter((job) => job.objectId === objectId);
    const objectKind = ["paragraph", "list", "table"][jobs[0]?.objectKind] || "unknown";
    const left = Math.min(...jobs.map((job) => job.bbox[0]));
    const top = Math.min(...jobs.map((job) => job.bbox[1]));
    const right = Math.max(...jobs.map((job) => job.bbox[2]));
    const bottom = Math.max(...jobs.map((job) => job.bbox[3]));
    const canvas = createCanvas(right - left, bottom - top);
    canvas
      .getContext("2d")
      .drawImage(
        sourceImage,
        left,
        top,
        right - left,
        bottom - top,
        0,
        0,
        right - left,
        bottom - top,
      );
    await writeFile(
      join(
        artifactRoot,
        "03-find-object",
        `object-${String(objectId + 1).padStart(3, "0")}-${objectKind}.png`,
      ),
      canvas.toBuffer("image/png"),
    );
  }
  await writeFile(
    join(artifactRoot, "07-generate-object", "result.md"),
    `${markdown.trim()}\n`,
  );
  await writeJson(join(artifactRoot, "route.json"), {
    schema: "ittm-rust-separated-debug-v1",
    runtime: "rust-wasm-node-adapter",
    route_id: debugRouteId,
    source: resolve(source),
    jobs: debugJobs,
  });
  const report: string[] = [
    "# Rust separated visual report",
    "",
    "- runtime: `rust-wasm-node-adapter`",
    `- route_id: \`0x${debugRouteId.toString(16).padStart(8, "0")}\``,
    "- warning: this is the browser WASM adapter under Node Canvas, not a real browser process",
    "",
    "## Source",
    "",
    '<img src="00-preprocess/raster.png" width="1200" alt="source">',
    "",
  ];
  for (const objectId of objectIds) {
    const objectJobs = debugJobs.filter((job) => job.objectId === objectId);
    const objectKind = ["paragraph", "list", "table"][objectJobs[0]?.objectKind] || "unknown";
    const objectName = `object-${String(objectId + 1).padStart(3, "0")}-${objectKind}.png`;
    report.push(
      `## Object ${objectId + 1} (type: ${objectKind})`,
      "",
      `<img src="03-find-object/${objectName}" width="1200" alt="object ${objectId + 1}">`,
      "",
    );
    for (const job of debugJobs.filter(
      (value) => value.objectId === objectId,
    )) {
      const stem = `block-${String(job.index + 1).padStart(3, "0")}`;
      report.push(
        `### OCR block ${job.index + 1}`,
        "",
        `<img src="04-separate-block/${stem}.png" width="1200" alt="OCR block ${job.index + 1}">`,
        "",
        "~~~text",
        job.text || "",
        "~~~",
        "",
      );
    }
  }
  report.push("## Generated Markdown", "", markdown.trim(), "");
  await writeFile(join(artifactRoot, "report.md"), `${report.join("\n")}\n`);
}

await prepareArtifactRoot();

try {
  const result = await runBrowserOcrLowMemory(
    file,
    () => undefined,
    undefined,
    profile,
    artifactRoot
      ? {
          async planned(jobs, routeId) {
            debugRouteId = routeId;
            debugJobs.push(...jobs.map((job) => ({ ...job })));
            await writeJson(
              join(artifactRoot, "04-separate-block", "manifest.json"),
              { stage: "separate-block", route_id: routeId, jobs },
            );
          },
          async block(image, job) {
            if (!debugJobs.some((value) => value.index === job.index)) {
              debugJobs.push({ ...job });
            }
            const stem = `block-${String(job.index + 1).padStart(3, "0")}`;
            const blockBytes = Buffer.from(await image.arrayBuffer());
            await writeFile(
              join(artifactRoot, "04-separate-block", `${stem}.png`),
              blockBytes,
            );
            await writeFile(
              join(artifactRoot, "05-ocr-blocks", `${stem}.png`),
              blockBytes,
            );
          },
          async recognized(text, confidenceMilli, job) {
            const target = debugJobs.find(
              (value) => value.index === job.index,
            );
            if (target) Object.assign(target, { text, confidenceMilli });
            const stem = `block-${String(job.index + 1).padStart(3, "0")}`;
            await writeFile(
              join(artifactRoot, "05-ocr-blocks", `${stem}.txt`),
              `${text.trim()}\n`,
            );
            await writeFile(
              join(
                artifactRoot,
                "06-get-segment",
                `segment-${String(job.index + 1).padStart(3, "0")}.txt`,
              ),
              `${text.trim()}\n`,
            );
          },
        }
      : undefined,
  );
  if (!result.markdown.trim()) {
    throw new Error("Browser OCR produced empty Markdown.");
  }
  const elapsedMs = Math.round(performance.now() - startedAt);
  const rssAfter = process.memoryUsage().rss;
  await finishArtifacts(result.markdown);
  process.stdout.write(
    JSON.stringify({
      markdown: result.markdown,
      elapsed_ms: elapsedMs,
      profile: profile.preprocessingProfile,
      flags: browserProfileFlags(profile).join("; "),
      pipeline_route_id: result.meta?.pipeline_route_id,
      rss_before_bytes: rssBefore,
      rss_after_bytes: rssAfter,
    }),
  );
} finally {
  await releaseBrowserOcrCache();
}
