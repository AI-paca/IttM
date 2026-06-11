import assert from "node:assert/strict";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const distRoot = path.resolve(process.argv[2] || "dist");
const expectedBase = normalizeBase(process.argv[3] || "/IttM/");
const vendorRoot = path.join(distRoot, "vendor", "tesseract");
const requiredTesseractAssets = [
  "worker.min.js",
  "tesseract-core-lstm.wasm.js",
  "tesseract-core-simd-lstm.wasm.js",
  "tesseract-core-relaxedsimd-lstm.wasm.js",
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

const indexHtml = await readFile(path.join(distRoot, "index.html"), "utf8");
assert.match(
  indexHtml,
  new RegExp(
    `(?:src|href)="${expectedBase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}assets/`,
  ),
  `index.html does not reference assets under ${expectedBase}`,
);

for (const asset of requiredTesseractAssets) {
  await assertNonEmpty(path.join(vendorRoot, asset));
}

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

console.log(
  `Pages build verified: ${expectedWorkerPath} and ${requiredTesseractAssets.length} local Tesseract assets`,
);
