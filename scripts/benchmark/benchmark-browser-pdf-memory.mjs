import { execFileSync } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const [url, pdfPath, outputPath, timeoutArg = "120"] = process.argv.slice(2);
if (!url || !pdfPath || !outputPath) {
  console.error(
    "Usage: benchmark-browser-pdf-memory.mjs URL PDF OUTPUT_JSON [TIMEOUT_SECONDS]",
  );
  process.exit(2);
}

const timeoutMs = Number(timeoutArg) * 1000;
const startedAt = Date.now();
const samples = [];
const events = [];
let extractionError = "";
let ocrWorkerStarted = false;

function chromiumRssBytes() {
  try {
    const output = execFileSync("ps", ["-eo", "comm=,rss="], {
      encoding: "utf8",
    });
    return output
      .split("\n")
      .map((line) => line.trim().split(/\s+/))
      .filter(([command]) => /chrome|chromium/i.test(command || ""))
      .reduce((total, fields) => {
        const rssKb = Number(fields.at(-1));
        return total + (Number.isFinite(rssKb) ? rssKb * 1024 : 0);
      }, 0);
  } catch {
    return 0;
  }
}

const browser = await chromium.launch({
  headless: true,
  args: ["--enable-precise-memory-info"],
});
const page = await browser.newPage();
const cdp = await page.context().newCDPSession(page);
await cdp.send("Performance.enable");

page.on("console", (message) => {
  if (
    message.type() === "error" &&
    message.text().startsWith("[OCR] Extraction failed:")
  ) {
    extractionError = message.text();
  }
  events.push({
    at_ms: Date.now() - startedAt,
    type: `console:${message.type()}`,
    text: message.text(),
  });
});
page.on("pageerror", (error) => {
  events.push({
    at_ms: Date.now() - startedAt,
    type: "pageerror",
    text: error.message,
  });
});
page.on("worker", (worker) => {
  if (worker.url().includes("/vendor/tesseract/")) {
    ocrWorkerStarted = true;
  }
  events.push({
    at_ms: Date.now() - startedAt,
    type: "worker",
    text: worker.url(),
  });
});

let sampling = true;
const sampler = setInterval(async () => {
  if (!sampling) return;
  try {
    const metrics = await cdp.send("Performance.getMetrics");
    const values = Object.fromEntries(
      metrics.metrics.map((metric) => [metric.name, metric.value]),
    );
    samples.push({
      at_ms: Date.now() - startedAt,
      chromium_rss_bytes: chromiumRssBytes(),
      js_heap_used_bytes: values.JSHeapUsedSize ?? 0,
      js_heap_total_bytes: values.JSHeapTotalSize ?? 0,
      documents: values.Documents ?? 0,
      nodes: values.Nodes ?? 0,
    });
  } catch {
    sampling = false;
  }
}, 500);

let outcome = "unknown";
let progress = "";
try {
  await page.goto(url, { waitUntil: "networkidle", timeout: 30_000 });
  await page.locator('input[type="file"]').setInputFiles(pdfPath);
  await page.getByRole("button", { name: "Browser", exact: true }).click();
  await page.getByRole("button", { name: "Получить текст" }).click();

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (extractionError) {
      outcome = "extraction-error";
      break;
    }
    const cancelButton = page.getByRole("button", { name: "Отменить" });
    if (await cancelButton.isVisible().catch(() => false)) {
      if (ocrWorkerStarted) {
        outcome = "first-page-rendered";
        await cancelButton.click();
        break;
      }
      progress = await page
        .locator("h2")
        .first()
        .innerText()
        .catch(() => "");
      if (
        /^(Проверка изображения|Распознавание скана).*страниц[а-я]* 1/i.test(
          progress,
        )
      ) {
        outcome = "first-page-rendered";
        await cancelButton.click();
        break;
      }
    }
    await page.waitForTimeout(50);
  }
  if (outcome === "unknown") outcome = "timeout";
  await page.waitForTimeout(2_000);
} catch (error) {
  outcome = "error";
  events.push({
    at_ms: Date.now() - startedAt,
    type: "script-error",
    text: error instanceof Error ? error.stack || error.message : String(error),
  });
} finally {
  sampling = false;
  clearInterval(sampler);
  await browser.close();
}

const peakRss = Math.max(
  0,
  ...samples.map((sample) => sample.chromium_rss_bytes),
);
const peakHeap = Math.max(
  0,
  ...samples.map((sample) => sample.js_heap_used_bytes),
);
await writeFile(
  outputPath,
  `${JSON.stringify(
    {
      url,
      pdf: pdfPath,
      outcome,
      progress,
      elapsed_ms: Date.now() - startedAt,
      peak_chromium_rss_bytes: peakRss,
      peak_main_js_heap_bytes: peakHeap,
      samples,
      events,
    },
    null,
    2,
  )}\n`,
);

if (outcome === "error") process.exitCode = 1;
