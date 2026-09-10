import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const distRoot = path.resolve(process.argv[2] || "dist");
const expectedBase = normalizeBase(process.argv[3] || "/IttM/");
const tesseractVendorRoot = path.join(distRoot, "vendor", "tesseract");
const pdfJsWasmVendorRoot = path.join(distRoot, "vendor", "pdfjs", "wasm");
const pipelineCorePath = path.join(distRoot, "wasm", "ittm_pipeline_core.wasm");
const textReviewerModelId = "HuggingFaceTB/SmolLM2-135M-Instruct";
const textReviewerModelRoot = path.join(
  distRoot,
  "vendor",
  "models",
  textReviewerModelId,
);
const textReviewerOnnxBytes = 137147981;
const requiredTesseractAssets = [
  "worker.min.js",
  "tesseract-core.wasm.js",
  "tesseract-core-simd.wasm.js",
  "tesseract-core-relaxedsimd.wasm.js",
  "tesseract-core-lstm.wasm.js",
  "tesseract-core-simd-lstm.wasm.js",
  "tesseract-core-relaxedsimd-lstm.wasm.js",
  "lang/eng.traineddata",
  "lang/rus.traineddata",
  "lang/chi_sim.traineddata",
  "lang/ell.traineddata",
  "lang/equ.traineddata",
];
const requiredPdfJsWasmAssets = [
  "jbig2.wasm",
  "jbig2_nowasm_fallback.js",
  "openjpeg.wasm",
  "openjpeg_nowasm_fallback.js",
  "qcms_bg.wasm",
  "quickjs-eval.js",
  "quickjs-eval.wasm",
];

function normalizeBase(base) {
  const withLeadingSlash = base.startsWith("/") ? base : `/${base}`;
  return withLeadingSlash.endsWith("/")
    ? withLeadingSlash
    : `${withLeadingSlash}/`;
}

async function listFiles(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const entryPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFiles(entryPath)));
    } else {
      files.push(entryPath);
    }
  }

  return files;
}

async function assertNonEmpty(filePath) {
  const fileStat = await stat(filePath);
  assert.ok(fileStat.size > 0, `${filePath} is empty`);
}

async function assertFileSize(filePath, expectedBytes) {
  const fileStat = await stat(filePath);
  assert.equal(
    fileStat.size,
    expectedBytes,
    `${filePath} has an unexpected size`,
  );
}

async function verifyServedPagesAssets(routes) {
  const server = createServer(async (request, response) => {
    try {
      const requestPath = decodeURIComponent(
        new URL(request.url || "/", "http://localhost").pathname,
      );
      if (!requestPath.startsWith(expectedBase)) {
        response.writeHead(404).end("Not found");
        return;
      }

      const relativePath =
        requestPath.slice(expectedBase.length) || "index.html";
      const filePath = path.resolve(distRoot, relativePath);
      const relativeToDist = path.relative(distRoot, filePath);
      if (relativeToDist.startsWith("..") || path.isAbsolute(relativeToDist)) {
        response.writeHead(400).end("Invalid path");
        return;
      }

      const content = await readFile(filePath);
      response.writeHead(200, {
        "content-type": filePath.endsWith(".html")
          ? "text/html"
          : "application/javascript",
      });
      response.end(content);
    } catch {
      response.writeHead(404).end("Not found");
    }
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  try {
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    const origin = `http://127.0.0.1:${address.port}`;

    for (const route of routes) {
      const response = await fetch(`${origin}${route}`);
      assert.equal(
        response.status,
        200,
        `${route} returned ${response.status}`,
      );
      const content = await response.arrayBuffer();
      assert.ok(content.byteLength > 0, `${route} returned an empty body`);
    }
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}

const indexHtml = await readFile(path.join(distRoot, "index.html"), "utf8");
assert.match(
  indexHtml,
  new RegExp(
    `(?:src|href)="${expectedBase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}assets/`,
  ),
  `index.html does not reference assets under ${expectedBase}`,
);

for (const asset of requiredTesseractAssets) {
  await assertNonEmpty(path.join(tesseractVendorRoot, asset));
}
for (const asset of requiredPdfJsWasmAssets) {
  await assertNonEmpty(path.join(pdfJsWasmVendorRoot, asset));
}
await assertNonEmpty(pipelineCorePath);
const pipelineCoreBytes = await readFile(pipelineCorePath);
const pipelineCore = await WebAssembly.instantiate(pipelineCoreBytes, {});
assert.equal(pipelineCore.instance.exports.ittm_pipeline_abi_version(), 6);
for (const name of [
  "memory",
  "ittm_alloc",
  "ittm_dealloc",
  "ittm_separated_begin",
  "ittm_separated_plan_begin",
  "ittm_separated_start_ocr",
  "ittm_separated_run_get_segment",
  "ittm_separated_job_count",
  "ittm_separated_job_field",
  "ittm_separated_set_ocr",
  "ittm_separated_add_ocr_word",
  "ittm_separated_add_ocr_word_ppm",
  "ittm_separated_render_length",
  "ittm_separated_render_copy",
  "ittm_separated_stage_mask",
  "ittm_separated_drop",
]) {
  assert.ok(
    pipelineCore.instance.exports[name],
    `Pages pipeline core misses ABI 6 export ${name}`,
  );
}
await assertNonEmpty(path.join(textReviewerModelRoot, "config.json"));
await assertFileSize(
  path.join(textReviewerModelRoot, "onnx", "model_quantized.onnx"),
  textReviewerOnnxBytes,
);

const javascriptFiles = (await listFiles(path.join(distRoot, "assets"))).filter(
  (filePath) => filePath.endsWith(".js"),
);
const bundleText = (
  await Promise.all(
    javascriptFiles.map((filePath) => readFile(filePath, "utf8")),
  )
).join("\n");
const expectedWorkerPath = `${expectedBase}vendor/tesseract/worker.min.js`;
const incorrectRootWorkerPath = "/vendor/tesseract/worker.min.js";
const expectedPdfJsWasmPath = `${expectedBase}vendor/pdfjs/wasm/`;
const pdfJsWasmRoute = "vendor/pdfjs/wasm/";
const modelVendorRoute = "vendor/models/";

assert.ok(
  bundleText.includes(expectedWorkerPath),
  `compiled bundle does not contain ${expectedWorkerPath}`,
);
if (expectedBase !== "/") {
  assert.ok(
    !bundleText.includes(`workerPath:"${incorrectRootWorkerPath}"`),
    `compiled bundle still contains root-relative ${incorrectRootWorkerPath}`,
  );
}
assert.ok(
  bundleText.includes(expectedBase) && bundleText.includes(pdfJsWasmRoute),
  `compiled bundle does not compose ${expectedPdfJsWasmPath}`,
);
assert.ok(
  bundleText.includes(expectedBase) &&
    bundleText.includes(modelVendorRoute) &&
    bundleText.includes(textReviewerModelId),
  `compiled reviewer worker does not reference ${textReviewerModelId}`,
);

await verifyServedPagesAssets([
  expectedBase,
  ...requiredTesseractAssets.map(
    (asset) => `${expectedBase}vendor/tesseract/${asset}`,
  ),
  ...requiredPdfJsWasmAssets.map(
    (asset) => `${expectedBase}vendor/pdfjs/wasm/${asset}`,
  ),
  `${expectedBase}wasm/ittm_pipeline_core.wasm`,
  `${expectedBase}vendor/models/${textReviewerModelId}/config.json`,
]);

console.log(
  `Pages build verified over HTTP: shared pipeline core, ${requiredTesseractAssets.length} Tesseract assets, ${requiredPdfJsWasmAssets.length} PDF.js decoder assets, and ${textReviewerModelId}`,
);
