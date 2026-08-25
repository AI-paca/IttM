import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const [
  url,
  pdfPath,
  outputPath,
  timeoutArg = "1200",
  sourceArg = "browser",
] = process.argv.slice(2);
if (!url || !pdfPath || !outputPath) {
  console.error(
    "Usage: benchmark-browser-pdf-e2e.mjs URL PDF OUTPUT_JSON [TIMEOUT_SECONDS] [browser|auto|local_tess|local_easy]",
  );
  process.exit(2);
}

const sourceLabels = {
  browser: "Browser Engine",
  auto: "Auto (Fallback)",
  local_tess: "Local Tesseract",
  local_easy: "Local EasyOCR",
};
const sourceLabel = sourceLabels[sourceArg];
if (!sourceLabel) {
  console.error(`Invalid source: ${sourceArg}`);
  process.exit(2);
}

const timeoutMs = Number(timeoutArg) * 1000;
if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
  console.error(`Invalid timeout: ${timeoutArg}`);
  process.exit(2);
}

const startedAt = Date.now();
const events = [];
const screenshotPath = outputPath.replace(/\.json$/i, ".png");
let extractionError = "";
let firstPageReadyAtMs = null;
let markdown = "";
let progress = "";
let busySeen = false;
let completionCandidateAt = null;
let completionCandidateLength = 0;
let currentPage = null;
let totalPages = null;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

page.on("console", (message) => {
  const text = message.text();
  if (
    message.type() === "error" &&
    text.startsWith("[OCR] Extraction failed:")
  ) {
    extractionError = text;
  }
  events.push({
    at_ms: Date.now() - startedAt,
    type: `console:${message.type()}`,
    text,
  });
});
page.on("pageerror", (error) => {
  events.push({
    at_ms: Date.now() - startedAt,
    type: "pageerror",
    text: error.message,
  });
});
page.on("requestfailed", (request) => {
  events.push({
    at_ms: Date.now() - startedAt,
    type: "requestfailed",
    text: `${request.url()} ${request.failure()?.errorText || ""}`.trim(),
  });
});

let outcome = "unknown";
try {
  await page.goto(url, { waitUntil: "networkidle", timeout: 30_000 });
  await page.locator('input[type="file"]').setInputFiles(pdfPath);
  const sourceCard = page.getByText(sourceLabel, { exact: true });
  if (!(await sourceCard.isVisible().catch(() => false))) {
    const settingsButton = page.getByRole("button", {
      name: "Настройки",
      exact: true,
    });
    if (await settingsButton.isVisible().catch(() => false)) {
      await settingsButton.click();
      await sourceCard.waitFor({ state: "visible", timeout: 5_000 });
    }
  }
  if (!(await sourceCard.isVisible().catch(() => false))) {
    throw new Error(`Requested source is unavailable: ${sourceLabel}`);
  }
  await sourceCard.click();
  const settingsOverlay = page.locator("div.fixed.inset-0.bg-overlay").first();
  if (await settingsOverlay.isVisible().catch(() => false)) {
    await settingsOverlay.click({ position: { x: 8, y: 8 } });
  }
  await page.getByRole("button", { name: "Получить текст" }).click();

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (extractionError) {
      outcome = "extraction-error";
      break;
    }

    const busyVisible = await page
      .getByRole("button", { name: /^(Остановить|Отменить)$/ })
      .isVisible()
      .catch(() => false);
    const bodyText = await page.locator("body").innerText().catch(() => "");
    const pageProgress = bodyText.match(/Страница\s+(\d+)\s+из\s+(\d+)/i);
    currentPage = pageProgress ? Number(pageProgress[1]) : null;
    totalPages = pageProgress ? Number(pageProgress[2]) : null;
    progress = pageProgress ? pageProgress[0] : "";
    markdown = await page
      .locator(".markdown-content")
      .innerText()
      .catch(() => "");

    if (busyVisible) {
      busySeen = true;
      completionCandidateAt = null;
    }

    if (
      firstPageReadyAtMs === null &&
      ((currentPage !== null && currentPage >= 1) || markdown.trim())
    ) {
      firstPageReadyAtMs = Date.now() - startedAt;
    }

    const allPagesReached =
      currentPage === null || totalPages === null || currentPage >= totalPages;
    if (busySeen && !busyVisible && allPagesReached && markdown.trim()) {
      if (
        completionCandidateAt === null ||
        completionCandidateLength !== markdown.length
      ) {
        completionCandidateAt = Date.now();
        completionCandidateLength = markdown.length;
      } else if (Date.now() - completionCandidateAt >= 1000) {
        outcome = "complete";
        break;
      }
    } else {
      completionCandidateAt = null;
    }

    await page.waitForTimeout(250);
  }

  if (outcome === "unknown") outcome = "timeout";
} catch (error) {
  outcome = "error";
  events.push({
    at_ms: Date.now() - startedAt,
    type: "script-error",
    text: error instanceof Error ? error.stack || error.message : String(error),
  });
} finally {
  await page
    .screenshot({
      path: screenshotPath,
      fullPage: true,
    })
    .catch((error) => {
      events.push({
        at_ms: Date.now() - startedAt,
        type: "screenshot-error",
        text: error instanceof Error ? error.message : String(error),
      });
    });
  await browser.close();
}

await writeFile(
  outputPath,
  `${JSON.stringify(
    {
      url,
      pdf: pdfPath,
      requested_source: sourceArg,
      requested_source_label: sourceLabel,
      outcome,
      progress,
      current_page: currentPage,
      total_pages: totalPages,
      busy_seen: busySeen,
      elapsed_ms: Date.now() - startedAt,
      first_page_ready_at_ms: firstPageReadyAtMs,
      markdown,
      events,
    },
    null,
    2,
  )}\n`,
);

if (outcome !== "complete") process.exitCode = 1;
