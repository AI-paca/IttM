import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const wasmPath = resolve(
  process.argv[2] ?? "web/public/wasm/ittm_pipeline_core.wasm",
);
const moduleBytes = await readFile(wasmPath);
const { instance } = await WebAssembly.instantiate(moduleBytes, {});
const exports = instance.exports;

const separatedExports = [
  "memory",
  "ittm_pipeline_route_id",
  "ittm_alloc",
  "ittm_dealloc",
  "ittm_separated_begin",
  "ittm_separated_plan_begin",
  "ittm_separated_start_ocr",
  "ittm_separated_run_get_segment",
  "ittm_separated_job_count",
  "ittm_separated_job_field",
  "ittm_separated_job_raster_field",
  "ittm_separated_job_raster_length",
  "ittm_separated_job_raster_copy",
  "ittm_separated_set_ocr",
  "ittm_separated_add_ocr_word",
  "ittm_separated_add_ocr_word_ppm",
  "ittm_separated_render_length",
  "ittm_separated_render_copy",
  "ittm_separated_stage_mask",
  "ittm_separated_drop",
  "ittm_pdf_native_begin",
  "ittm_pdf_native_add_item",
  "ittm_pdf_native_build",
  "ittm_pdf_native_render_length",
  "ittm_pdf_native_render_copy",
  "ittm_pdf_native_drop",
];
for (const name of separatedExports) {
  if (!exports[name]) throw new Error(`WASM ABI 6 misses ${name}`);
}

if (exports.ittm_pipeline_abi_version() !== 6) {
  throw new Error("Unexpected pipeline core ABI version");
}
if (exports.ittm_pipeline_route_id() !== 0x52530003) {
  throw new Error("Unexpected separated route id");
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

const nativePdfHandle = exports.ittm_pdf_native_begin();
if (!nativePdfHandle)
  throw new Error("WASM native PDF route rejected a session");
try {
  for (const [index, [text, x, y]] of [
    ["A", 10, 100],
    ["B", 80, 100],
    ["C", 10, 70],
    ["D", 80, 70],
  ].entries()) {
    const encoded = new TextEncoder().encode(text);
    const pointer = exports.ittm_alloc(encoded.byteLength);
    new Uint8Array(exports.memory.buffer, pointer, encoded.byteLength).set(
      encoded,
    );
    const status = exports.ittm_pdf_native_add_item(
      nativePdfHandle,
      pointer,
      encoded.byteLength,
      20,
      10,
      1,
      1,
      0,
      0,
      1,
      x,
      y,
    );
    exports.ittm_dealloc(pointer, encoded.byteLength);
    if (status !== 0) {
      throw new Error(
        `WASM native PDF item ${index} failed with status ${status}`,
      );
    }
  }
  if (exports.ittm_pdf_native_build(nativePdfHandle) !== 0) {
    throw new Error("WASM native PDF build failed");
  }
  const length = exports.ittm_pdf_native_render_length(nativePdfHandle);
  const pointer = exports.ittm_alloc(length);
  const copied = exports.ittm_pdf_native_render_copy(
    nativePdfHandle,
    pointer,
    length,
  );
  const artifact = JSON.parse(
    new TextDecoder().decode(
      new Uint8Array(exports.memory.buffer, pointer, length),
    ),
  );
  exports.ittm_dealloc(pointer, length);
  if (
    copied !== length ||
    artifact.objects?.[0]?.kind !== "table" ||
    artifact.segments?.length !== 4
  ) {
    throw new Error("WASM native PDF object/segment parity failed");
  }
} finally {
  if (exports.ittm_pdf_native_drop(nativePdfHandle) !== 0) {
    throw new Error("WASM native PDF session cleanup failed");
  }
}

const trustedMarkdownCapabilities = 1 | 2 | 4;
const recognizeSegmentsBit = 1 << 3;
if (
  exports.ittm_pipeline_recipe_mask(trustedMarkdownCapabilities) !==
  recognizeSegmentsBit
) {
  throw new Error("WASM trusted API recipe parity failed");
}

const width = 80;
const height = 60;
const pixels = new Uint8Array(width * height).fill(255);
for (const [top, bottom, left, right] of [
  [7, 15, 8, 55],
  [40, 48, 12, 70],
]) {
  for (let y = top; y < bottom; y += 1) {
    pixels.fill(0, y * width + left, y * width + right);
  }
}
const pixelPointer = exports.ittm_alloc(pixels.byteLength);
new Uint8Array(exports.memory.buffer, pixelPointer, pixels.byteLength).set(
  pixels,
);
const handle = exports.ittm_separated_plan_begin(
  pixelPointer,
  pixels.byteLength,
  width,
  height,
  width,
  1,
);
exports.ittm_dealloc(pixelPointer, pixels.byteLength);
if (!handle) throw new Error("WASM separated route rejected parity raster");
try {
  if (exports.ittm_separated_stage_mask(handle) !== 0b00011111) {
    throw new Error("WASM separated planning stage mask failed");
  }
  if (exports.ittm_separated_job_count(handle) !== 0) {
    throw new Error("WASM planning stage leaked an OCR job");
  }
  if (exports.ittm_separated_start_ocr(handle) !== 1) {
    throw new Error("WASM explicit OCR stage did not start");
  }
  if (exports.ittm_separated_job_count(handle) !== 1) {
    throw new Error("WASM separated OCR job count parity failed");
  }
  const firstBox = Array.from({ length: 4 }, (_unused, field) =>
    exports.ittm_separated_job_field(handle, 0, field),
  );
  if (
    firstBox[0] > 8 ||
    firstBox[1] > 7 ||
    firstBox[2] < 55 ||
    firstBox[3] < 15 ||
    firstBox.join(",") === `0,0,${width},${height}`
  ) {
    throw new Error(`WASM separated geometry parity failed: ${firstBox}`);
  }
  if (exports.ittm_separated_job_field(handle, 0, 7) !== 1) {
    throw new Error("WASM separated context span parity failed");
  }
  const setOcr = (index, text) => {
    const encoded = new TextEncoder().encode(text);
    const pointer = exports.ittm_alloc(encoded.byteLength);
    new Uint8Array(exports.memory.buffer, pointer, encoded.byteLength).set(
      encoded,
    );
    const wordStatus = exports.ittm_separated_add_ocr_word_ppm(
      handle,
      index,
      pointer,
      encoded.byteLength,
      0,
      0,
      1,
      1,
      1_000_000,
    );
    const textStatus = exports.ittm_separated_set_ocr(
      handle,
      index,
      pointer,
      encoded.byteLength,
      0,
    );
    exports.ittm_dealloc(pointer, encoded.byteLength);
    if (wordStatus !== 0 || textStatus !== 0) {
      throw new Error(
        `WASM OCR handoff failed: word=${wordStatus}, text=${textStatus}`,
      );
    }
  };
  setOcr(0, "first");
  if (exports.ittm_separated_job_count(handle) !== 2) {
    throw new Error("WASM recursive job expansion parity failed");
  }
  const secondBox = Array.from({ length: 4 }, (_unused, field) =>
    exports.ittm_separated_job_field(handle, 1, field),
  );
  if (
    secondBox[0] > 12 ||
    secondBox[1] > 40 ||
    secondBox[2] < 70 ||
    secondBox[3] < 48
  ) {
    throw new Error(`WASM recursive geometry parity failed: ${secondBox}`);
  }
  setOcr(1, "second");
  if (exports.ittm_separated_run_get_segment(handle) !== 1) {
    throw new Error("WASM explicit get-segment stage failed");
  }
  const length = exports.ittm_separated_render_length(handle);
  const outputPointer = exports.ittm_alloc(length);
  const copied = exports.ittm_separated_render_copy(
    handle,
    outputPointer,
    length,
  );
  const output = new TextDecoder().decode(
    new Uint8Array(exports.memory.buffer, outputPointer, length),
  );
  exports.ittm_dealloc(outputPointer, length);
  if (copied !== length || output !== "# result\n\nfirst\n\nsecond") {
    throw new Error("WASM separated assembly parity failed");
  }
  if (exports.ittm_separated_stage_mask(handle) !== 0b11111111) {
    throw new Error("WASM separated completion stage mask failed");
  }
} finally {
  if (exports.ittm_separated_drop(handle) !== 0) {
    throw new Error("WASM separated session cleanup failed");
  }
}

console.log("pipeline core WASM exports and separated route: pass");
