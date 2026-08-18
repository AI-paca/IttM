import { execFile } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify } from "node:util";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";

import {
  buildNativePdfOracle,
  type NativePdfOracle,
  type PdfTextGeometryItem,
} from "../../web/src/lib/pdf-native-oracle";

const execFileAsync = promisify(execFile);

const [pdfArg, outputArg] = process.argv.slice(2);
if (!pdfArg || !outputArg) {
  console.error(
    "Usage: node --import tsx scripts/benchmark/benchmark-native-pdf-oracle.ts PDF OUTPUT_JSON",
  );
  process.exit(2);
}

const pdfPath = resolve(pdfArg);
const outputPath = resolve(outputArg);
const svgPath = outputPath.replace(/\.json$/i, ".svg");
const startedAt = performance.now();
const data = new Uint8Array(await readFile(pdfPath));
const task = getDocument({ data, useSystemFonts: false });
const document = await task.promise;
interface PageArtifact {
  page: number;
  width: number;
  height: number;
  route: "trusted_native_text_bypass";
  oracle: NativePdfOracle | null;
}
const pages: PageArtifact[] = [];

try {
  for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber += 1) {
    const page = await document.getPage(pageNumber);
    try {
      const content = await page.getTextContent();
      const viewport = page.getViewport({ scale: 1 });
      pages.push({
        page: pageNumber,
        width: viewport.width,
        height: viewport.height,
        route: "trusted_native_text_bypass" as const,
        oracle: buildNativePdfOracle(
          content.items as unknown as PdfTextGeometryItem[],
        ),
      });
    } finally {
      page.cleanup();
    }
  }
} finally {
  await document.cleanup();
  await task.destroy();
}

function escapeXml(value: unknown): string {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function overlayForPage(
  page: PageArtifact,
  rasterDataUrl: string,
): string {
  const { oracle } = page;
  const colors = {
    paragraph: "#087f5b",
    table: "#d9480f",
    small_table: "#1864ab",
  } as const;
  const segmentText = new Map<string, string[]>();
  for (const segment of oracle?.nativeSegmentExtraction.segments ?? []) {
    const topology = segment.topology;
    const key = `${topology.object_id}:${topology.row}:${topology.column}`;
    const values = segmentText.get(key) ?? [];
    values.push(segment.text);
    segmentText.set(key, values);
  }
  const cells = (oracle?.nativeFindObject.objects ?? []).flatMap((object) =>
    object.cells.map((cell) => {
      const color = colors[object.kind];
      const y = page.height - cell.bbox.bottom;
      const width = Math.max(0.5, cell.bbox.right - cell.bbox.left);
      const height = Math.max(0.5, cell.bbox.bottom - cell.bbox.top);
      const key = `${object.objectId}:${cell.row}:${cell.column}`;
      const title = `${object.objectId} ${object.kind} row=${cell.row} column=${cell.column} row_span=${cell.rowSpan} column_span=${cell.columnSpan}: ${(segmentText.get(key) ?? []).join(" ")}`;
      return `<g>
<title>${escapeXml(title)}</title>
<rect x="${cell.bbox.left}" y="${y}" width="${width}" height="${height}" fill="${color}" fill-opacity="0.08" stroke="${color}" stroke-width="0.35" vector-effect="non-scaling-stroke"/>
<text x="${cell.bbox.left + 0.7}" y="${Math.max(3, y + 3)}" font-size="2.6" fill="${color}" stroke="white" stroke-width="0.8" paint-order="stroke">r${cell.row}c${cell.column} ${cell.rowSpan}x${cell.columnSpan}</text>
</g>`;
    }),
  );
  const objects = (oracle?.nativeFindObject.objects ?? []).map((object) => {
    const color = colors[object.kind];
    const y = page.height - object.bbox.bottom;
    return `<g>
<rect x="${object.bbox.left}" y="${y}" width="${object.bbox.right - object.bbox.left}" height="${object.bbox.bottom - object.bbox.top}" fill="none" stroke="${color}" stroke-width="1.8" vector-effect="non-scaling-stroke"/>
<text x="${object.bbox.left + 2}" y="${Math.max(10, y + 9)}" font-size="8" font-weight="700" fill="${color}" stroke="white" stroke-width="2.5" paint-order="stroke">${escapeXml(object.objectId)} ${object.kind}</text>
</g>`;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${Math.ceil(page.width * 2)}" height="${Math.ceil(page.height * 2)}" viewBox="0 0 ${page.width} ${page.height}">
<image href="${rasterDataUrl}" x="0" y="0" width="${page.width}" height="${page.height}" preserveAspectRatio="none"/>
${cells.join("\n")}
${objects.join("\n")}
</svg>\n`;
}

const overlayArtifacts = [];
const outputStem = outputPath.replace(/\.json$/i, "");
for (const page of pages) {
  const suffix = `page-${String(page.page).padStart(3, "0")}`;
  const rasterPrefix = `${outputStem}.${suffix}`;
  await execFileAsync("pdftoppm", [
    "-f",
    String(page.page),
    "-l",
    String(page.page),
    "-singlefile",
    "-r",
    "72",
    "-png",
    pdfPath,
    rasterPrefix,
  ]);
  const rasterPath = `${rasterPrefix}.png`;
  const overlayPath = `${rasterPrefix}.overlay.svg`;
  const rasterDataUrl = `data:image/png;base64,${(await readFile(rasterPath)).toString("base64")}`;
  await writeFile(overlayPath, overlayForPage(page, rasterDataUrl));
  overlayArtifacts.push({ page: page.page, raster: rasterPath, overlay: overlayPath });
}

const artifact = {
  schema: "ittm.native-pdf-oracle-run/v1",
  pdf: pdfPath,
  elapsed_ms: Math.round(performance.now() - startedAt),
  route: "trusted_native_text_bypass",
  early_stage_ownership: {
    find_object: "native_pdf_text_bbox",
    extract_segments: "native_pdf_text_bbox",
    raster_geometry_used: false,
    ocr_blocks_used: false,
  },
  common_boundary: "segment_id + topology + text",
  overlays: overlayArtifacts,
  pages,
};

const cards = pages.map(({ page, oracle }, index) => {
  const y = 70 + index * 145;
  const lines = oracle
    ? [
        `native objects: ${oracle.nativeFindObject.objects.length}`,
        `native bbox segments: ${oracle.nativeSegmentExtraction.segments.length}`,
        `common handoff segments: ${oracle.assemblerHandoff.segments.length}`,
        `assembled objects: ${oracle.assembled.objects.length}; markdown chars: ${oracle.assembled.markdown.length}`,
        `overlay: ${overlayArtifacts[index]?.overlay ?? "unavailable"}`,
      ]
    : ["no real text layer; native oracle unavailable"];
  return `<g transform="translate(35 ${y})">
<rect width="1330" height="120" rx="14" fill="#fffaf0" stroke="#244e3f" stroke-width="2"/>
<text x="20" y="28" class="title">Page ${page}: ${oracle ? "trusted native text oracle" : "non-oracle"}</text>
${lines.map((line, lineIndex) => `<text x="20" y="${52 + lineIndex * 20}">${escapeXml(line)}</text>`).join("\n")}
</g>`;
});
const height = Math.max(250, 90 + pages.length * 145);
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="${height}" viewBox="0 0 1400 ${height}">
<rect width="100%" height="100%" fill="#e8f0e8"/>
<style>text{font:16px 'DejaVu Sans Mono',monospace;fill:#18332a}.heading{font-size:24px;font-weight:700}.title{font-weight:700}</style>
<text x="35" y="38" class="heading">Native PDF oracle to shared assembler</text>
${cards.join("\n")}
</svg>\n`;

await writeFile(outputPath, `${JSON.stringify(artifact, null, 2)}\n`);
await writeFile(svgPath, svg);

if (pages.every((page) => page.oracle === null)) {
  console.error("No native text-layer oracle was found.");
  process.exitCode = 1;
} else {
  console.log(
    JSON.stringify({
      output: outputPath,
      svg: svgPath,
      elapsed_ms: artifact.elapsed_ms,
      pages: pages.length,
      oracle_pages: pages.filter((page) => page.oracle !== null).length,
      overlays: overlayArtifacts.map((overlay) => overlay.overlay),
    }),
  );
}
