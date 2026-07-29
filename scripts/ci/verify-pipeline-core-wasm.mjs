import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const wasmPath = resolve(
  process.argv[2] ?? "web/public/wasm/ittm_pipeline_core.wasm",
);
const moduleBytes = await readFile(wasmPath);
const { instance } = await WebAssembly.instantiate(moduleBytes, {});
const exports = instance.exports;

if (exports.ittm_pipeline_abi_version() !== 3) {
  throw new Error("Unexpected pipeline core ABI version");
}
if (exports.ittm_span_evidence_score(800, 900, 700, 2, 0) !== 71500) {
  throw new Error("WASM span evidence parity failed");
}
if (exports.ittm_span_evidence_score(800, 900, 700, 2, 1) !== 56500) {
  throw new Error("WASM span contradiction penalty failed");
}
if (exports.ittm_sparse_add_signal(3, 11) !== 14) {
  throw new Error("WASM sparse-code parity failed");
}
if (exports.ittm_is_isolated_heading(2, 18, 12, 3) !== 1) {
  throw new Error("WASM structural classification parity failed");
}
if (exports.ittm_is_isolated_heading(1, 2, 2, 3) !== 0) {
  throw new Error("WASM structural noise rejection failed");
}
if (exports.ittm_should_replace_primary(100, 180, 10, 8) !== 1) {
  throw new Error("WASM evidence-preserving fallback selection failed");
}
if (exports.ittm_should_replace_primary(100, 180, 10, 7) !== 0) {
  throw new Error("WASM lossy fallback rejection failed");
}
if (exports.ittm_should_drop_text_block(100, 100, 9, 10, 10, 0) !== 1) {
  throw new Error("WASM overlap dedup failed");
}
if (exports.ittm_should_drop_text_block(100, 100, 8, 10, 10, 879) !== 0) {
  throw new Error("WASM distinct block preservation failed");
}

const trustedMarkdownCapabilities = 1 | 2 | 4;
const recognizeSegmentsBit = 1 << 3;
if (
  exports.ittm_pipeline_recipe_mask(trustedMarkdownCapabilities) !==
  recognizeSegmentsBit
) {
  throw new Error("WASM trusted API recipe parity failed");
}

console.log("pipeline core WASM exports: pass");
