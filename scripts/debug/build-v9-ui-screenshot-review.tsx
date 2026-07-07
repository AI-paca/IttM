import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarkdownContent } from "../../web/src/ui/MarkdownContent";
import { analyzeMarkdownGrammar } from "../../web/src/ui/markdown-diagnostics";

type Engine = "tesseract" | "easyocr" | "browser-tesseract";

type Case = {
  engine: Engine;
  suite: string;
  file: string;
  markdownPath: string;
  referencePath: string | null;
  sourceImagePath: string | null;
  score: Score | null;
};

type Score = {
  text: string;
  compact: string;
  grammar: string;
  success: string;
  gate: string;
  notes: string;
};

type Suite = {
  name: string;
  tmpRoot: string;
  artifactRoot: string;
};

const ENGINES: Engine[] = ["tesseract", "easyocr", "browser-tesseract"];
const DEFAULT_OUTPUT_ROOT = "debug/private/v9-ui-screenshots";
const DEFAULT_SUITES: Suite[] = [
  {
    name: "full-pdf-as-png",
    tmpRoot: "debug/tmp/full-pdf-as-png",
    artifactRoot: "debug/artifacts/full-pdf-as-png",
  },
  {
    name: "image-fixtures",
    tmpRoot: "debug/tmp/image-fixtures",
    artifactRoot: "debug/artifacts/image-fixtures",
  },
];
const IMPORTANT_PATTERNS = [
  /Ucheb_plan/i,
  /УП2022/i,
  /battle_ucheb_plan_header/i,
  /SAMPLE_4k/i,
  /SAMPLE_mixed/i,
  /image(?: \([78]\))?\.png$/i,
  /photo_2026-06-26/i,
];

function usage(): never {
  console.error(
    [
      "Usage:",
      "  node --import tsx scripts/debug/build-v9-ui-screenshot-review.tsx [--output-root DIR] [--suite NAME:TMP:ARTIFACT] [--engine ENGINE] [--file TEXT] [--all-files] [--no-screenshots]",
    ].join("\n"),
  );
  process.exit(2);
}

function parseSuiteArg(value: string): Suite {
  const [name, tmpRoot, artifactRoot, ...rest] = value.split(":");
  if (!name || !tmpRoot || !artifactRoot || rest.length > 0) usage();
  return { name, tmpRoot, artifactRoot };
}

function parseArgs(): {
  outputRoot: string;
  suites: Suite[];
  engines: Engine[];
  fileFilters: string[];
  allFiles: boolean;
  screenshots: boolean;
} {
  const args = process.argv.slice(2);
  let outputRoot = DEFAULT_OUTPUT_ROOT;
  const suites: Suite[] = [];
  const engines = new Set<Engine>();
  const fileFilters: string[] = [];
  let allFiles = false;
  let screenshots = true;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--output-root") {
      outputRoot = args[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (arg === "--suite") {
      suites.push(parseSuiteArg(args[index + 1] ?? ""));
      index += 1;
      continue;
    }
    if (arg === "--engine") {
      const engine = args[index + 1] as Engine | undefined;
      if (!engine || !ENGINES.includes(engine)) usage();
      engines.add(engine);
      index += 1;
      continue;
    }
    if (arg === "--file") {
      const filter = args[index + 1] ?? "";
      if (!filter) usage();
      fileFilters.push(filter);
      index += 1;
      continue;
    }
    if (arg === "--all-files") {
      allFiles = true;
      continue;
    }
    if (arg === "--no-screenshots") {
      screenshots = false;
      continue;
    }
    usage();
  }
  if (!outputRoot) usage();
  return {
    outputRoot: resolve(outputRoot),
    suites: suites.length > 0 ? suites : DEFAULT_SUITES,
    engines: engines.size > 0 ? [...engines] : ENGINES,
    fileFilters,
    allFiles,
    screenshots,
  };
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fileUrl(path: string): string {
  return `file://${resolve(path)
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/")}`;
}

function slug(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9а-яА-ЯёЁ._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 180);
}

function readResultBody(path: string): string {
  const text = readFileSync(path, "utf8");
  const separator = "\n---\n";
  if (text.startsWith("# ") && text.includes(separator)) {
    return text.split(separator, 2)[1].trim();
  }
  return text.trim();
}

function csvRows(path: string): Record<string, string>[] {
  if (!existsSync(path)) return [];
  const text = readFileSync(path, "utf8");
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (char !== "\r") {
      cell += char;
    }
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  const [header, ...body] = rows;
  if (!header) return [];
  return body
    .filter((values) => values.some((value) => value.trim()))
    .map((values) =>
      Object.fromEntries(
        header.map((name, index) => [name, values[index] ?? ""]),
      ),
    );
}

function scoreMap(artifactRoot: string): Map<string, Record<Engine, Score>> {
  const rows = csvRows(join(artifactRoot, "result.csv"));
  const result = new Map<string, Record<Engine, Score>>();
  for (const row of rows) {
    const file = row.file;
    if (!file) continue;
    const scores = result.get(file) ?? ({} as Record<Engine, Score>);
    for (const engine of ENGINES) {
      scores[engine] = {
        text: row[`${engine} %`] || "n/a",
        compact: row[`${engine} compact quality %`] || "n/a",
        grammar: row[`${engine} markdown grammar %`] || "n/a",
        success: row[`${engine} success probability %`] || "n/a",
        gate: row[`${engine} gate`] || "n/a",
        notes: row[`${engine} markdown grammar notes`] || "",
      };
    }
    result.set(file, scores);
  }
  return result;
}

function referencePath(tmpRoot: string, file: string): string | null {
  const candidates = [
    join("debug/reference", `${file}.md`),
    join(tmpRoot, "combined-reference", `${file}.md`),
    join(tmpRoot, "pdf-image-reference", `${file}.md`),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

function sourceImagePath(tmpRoot: string, file: string): string | null {
  const candidates = [
    join(tmpRoot, "fixtures", file),
    join("debug/fixtures", file),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

function collectCases(
  suites: Suite[],
  engines: Engine[],
  fileFilters: string[],
  allFiles: boolean,
): Case[] {
  const cases: Case[] = [];
  for (const suite of suites) {
    if (!existsSync(suite.tmpRoot)) continue;
    const scores = scoreMap(suite.artifactRoot);
    for (const engine of engines) {
      const engineRoot = join(suite.tmpRoot, engine);
      if (!existsSync(engineRoot)) continue;
      for (const entry of readdirSync(engineRoot, { withFileTypes: true })) {
        if (!entry.isFile() || !entry.name.endsWith(".md")) continue;
        const file = entry.name.slice(0, -".md".length);
        if (
          !allFiles &&
          !IMPORTANT_PATTERNS.some((pattern) => pattern.test(file))
        ) {
          continue;
        }
        if (
          fileFilters.length > 0 &&
          !fileFilters.some((filter) => file.includes(filter))
        ) {
          continue;
        }
        cases.push({
          engine,
          suite: suite.name,
          file,
          markdownPath: join(engineRoot, entry.name),
          referencePath: referencePath(suite.tmpRoot, file),
          sourceImagePath: sourceImagePath(suite.tmpRoot, file),
          score: scores.get(file)?.[engine] ?? null,
        });
      }
    }
  }
  return cases.sort((left, right) =>
    [left.engine, left.suite, left.file]
      .join("\0")
      .localeCompare([right.engine, right.suite, right.file].join("\0"), "ru"),
  );
}

function css(): string {
  return `
    :root {
      color-scheme: light;
      --bg: #eef2f7;
      --surface: #fff;
      --border: #cfd8e3;
      --text: #17212f;
      --muted: #5f6f83;
      --thead: #e7edf5;
      --bad: #a5282c;
      --good: #16734a;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main.review {
      width: 100vw;
      height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 10px;
      padding: 12px;
    }
    .topbar {
      min-height: 58px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }
    h1 {
      margin: 0;
      font-size: 17px;
      line-height: 1.2;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }
    .meta {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .score {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      font-size: 12px;
      font-weight: 800;
    }
    .score span {
      padding: 5px 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f7f9fc;
      white-space: nowrap;
    }
    .score .fail { color: var(--bad); }
    .score .pass { color: var(--good); }
    .panes {
      min-height: 0;
      display: grid;
      grid-template-columns: 0.9fr 1.05fr 1.2fr;
      gap: 10px;
    }
    .pane {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: auto 1fr;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .pane h2 {
      margin: 0;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      background: #f7f9fc;
      font-size: 13px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .pane-body {
      min-height: 0;
      overflow: auto;
      padding: 10px;
    }
    .source-body {
      padding: 0;
      display: grid;
      place-items: start center;
      background: #dfe6ef;
    }
    .source-body img {
      width: 100%;
      height: auto;
      display: block;
    }
    .missing {
      color: var(--muted);
      font-size: 14px;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }
    .toolbar button {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      padding: 7px 10px;
    }
    .grammar-panel {
      margin-bottom: 10px;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f7f9fc;
      font-size: 13px;
    }
    .grammar-panel.is-hidden {
      display: none;
    }
    .grammar-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
      margin-top: 8px;
    }
    .grammar-metric {
      padding: 6px 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }
    .grammar-metric b {
      display: block;
      font-size: 10px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .grammar-metric span {
      display: block;
      margin-top: 2px;
      font-size: 15px;
      font-weight: 900;
    }
    article[data-role="result"] {
      min-height: 100%;
      padding: 10px;
      border: 2px solid #44546a;
      border-radius: 8px;
      background: var(--surface);
    }
    .markdown-content { max-width: none; font-size: 13px; line-height: 1.45; }
    .markdown-content__table-wrap {
      width: 100%;
      max-height: none;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    table {
      width: max-content;
      min-width: 100%;
      border-collapse: collapse;
      font-size: 0.8em;
      line-height: 1.25;
    }
    th, td {
      padding: 5px 7px;
      border-right: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
      vertical-align: top;
      text-align: left;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--thead);
    }
    pre {
      overflow-x: auto;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f7f9fc;
    }
    .fullscreen-capture main.review {
      padding: 0;
      grid-template-rows: 1fr;
    }
    .fullscreen-capture .topbar,
    .fullscreen-capture .source-pane,
    .fullscreen-capture .reference-pane,
    .fullscreen-capture .result-pane > h2 {
      display: none;
    }
    .fullscreen-capture .panes {
      display: block;
      height: 100vh;
    }
    .fullscreen-capture .result-pane {
      display: grid;
      height: 100vh;
      border: 0;
      border-radius: 0;
    }
    .fullscreen-capture .result-pane .pane-body {
      min-height: 0;
      display: grid;
      grid-template-rows: auto auto 1fr;
      padding: 12px;
      overflow: hidden;
    }
    .fullscreen-capture article[data-role="result"] {
      min-height: 0;
      overflow: auto;
    }
  `;
}

function grammarHtml(markdown: string): string {
  const diagnostics = analyzeMarkdownGrammar(markdown);
  const messages = [...diagnostics.errors, ...diagnostics.warnings]
    .slice(0, 8)
    .map((message) => `<li>${escapeHtml(message)}</li>`)
    .join("");
  return `
    <div><strong>Status:</strong> ${escapeHtml(diagnostics.status)}</div>
    <div class="grammar-grid">
      <div class="grammar-metric"><b>Tables</b><span>${diagnostics.tableCount}</span></div>
      <div class="grammar-metric"><b>Rows</b><span>${diagnostics.tableRows}</span></div>
      <div class="grammar-metric"><b>Max cols</b><span>${diagnostics.maxColumns}</span></div>
      <div class="grammar-metric"><b>Merges</b><span>${diagnostics.mergeMarkers}</span></div>
    </div>
    ${messages ? `<ul>${messages}</ul>` : ""}
  `;
}

function markdownHtml(path: string | null, fallback: string): string {
  if (path === null) return `<p class="missing">${escapeHtml(fallback)}</p>`;
  const body = readResultBody(path);
  return renderToStaticMarkup(<MarkdownContent>{body}</MarkdownContent>);
}

function scoreHtml(score: Score | null): string {
  if (score === null) {
    return `<div class="score"><span>score: n/a</span></div>`;
  }
  const gateClass = score.gate === "pass" ? "pass" : "fail";
  return `
    <div class="score">
      <span>text ${escapeHtml(score.text)}</span>
      <span>compact ${escapeHtml(score.compact)}</span>
      <span>grammar ${escapeHtml(score.grammar)}</span>
      <span>success ${escapeHtml(score.success)}</span>
      <span class="${gateClass}">gate ${escapeHtml(score.gate)}</span>
    </div>
  `;
}

function interactionScript(): string {
  return `
    const grammarButton = document.querySelector('[data-action="grammar"]');
    const fullscreenButton = document.querySelector('[data-action="fullscreen"]');
    const grammarPanel = document.querySelector('[data-role="grammar-panel"]');
    const result = document.querySelector('[data-role="result"]');
    grammarButton?.addEventListener('click', () => {
      grammarPanel?.classList.toggle('is-hidden');
    });
    fullscreenButton?.addEventListener('click', async () => {
      if (!result) return;
      if (document.fullscreenElement === result) {
        await document.exitFullscreen?.();
      } else {
        await result.requestFullscreen?.();
      }
    });
  `;
}

function caseHtml(item: Case, mode: "compare" | "fullscreen"): string {
  const body = readResultBody(item.markdownPath);
  const rendered = renderToStaticMarkup(
    <MarkdownContent>{body}</MarkdownContent>,
  );
  const source = item.sourceImagePath
    ? `<img src="${escapeHtml(fileUrl(item.sourceImagePath))}" alt="${escapeHtml(item.file)}">`
    : `<p class="missing">source image not found</p>`;
  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(item.engine)} ${escapeHtml(item.file)}</title>
  <style>${css()}</style>
</head>
<body class="${mode === "fullscreen" ? "fullscreen-capture" : ""}">
  <main class="review">
    <header class="topbar">
      <div>
        <h1>${escapeHtml(item.engine)} / ${escapeHtml(item.suite)} / ${escapeHtml(item.file)}</h1>
        <div class="meta">${escapeHtml(item.markdownPath)}</div>
        <div class="meta">${escapeHtml(item.score?.notes ?? "")}</div>
      </div>
      ${scoreHtml(item.score)}
    </header>
    <section class="panes">
      <section class="pane source-pane">
        <h2>Исходная картинка</h2>
        <div class="pane-body source-body">${source}</div>
      </section>
      <section class="pane reference-pane">
        <h2>Reference Markdown</h2>
        <div class="pane-body markdown-content">${markdownHtml(item.referencePath, "reference markdown not found")}</div>
      </section>
      <section class="pane result-pane">
        <h2>UI result fullscreen surface</h2>
        <div class="pane-body">
          <div class="toolbar">
            <button type="button" data-action="grammar">Грамматика</button>
            <button type="button" data-action="fullscreen">Полный экран</button>
          </div>
          <section class="grammar-panel" data-role="grammar-panel">${grammarHtml(body)}</section>
          <article data-role="result">${rendered}</article>
        </div>
      </section>
    </section>
  </main>
  <script>${interactionScript()}</script>
</body>
</html>`;
}

function writeReviewPages(outputRoot: string, cases: Case[]): Case[] {
  mkdirSync(outputRoot, { recursive: true });
  mkdirSync(join(outputRoot, "all"), { recursive: true });
  mkdirSync(join(outputRoot, "all", "fullscreen"), { recursive: true });
  for (const engine of ENGINES) {
    mkdirSync(join(outputRoot, engine), { recursive: true });
    mkdirSync(join(outputRoot, engine, "fullscreen"), { recursive: true });
  }

  const links: string[] = [];
  for (const item of cases) {
    const name = `${slug(item.suite)}__${slug(item.file)}.html`;
    const fullscreenName = `${slug(item.suite)}__${slug(item.file)}.fullscreen.html`;
    const engineDir = join(outputRoot, item.engine);
    const htmlPath = join(engineDir, name);
    writeFileSync(htmlPath, caseHtml(item, "compare"), "utf8");
    writeFileSync(
      join(engineDir, "fullscreen", fullscreenName),
      caseHtml(item, "fullscreen"),
      "utf8",
    );
    links.push(
      `<li><a href="./${item.engine}/${escapeHtml(name)}">${escapeHtml(item.engine)} / ${escapeHtml(item.suite)} / ${escapeHtml(item.file)}</a> <a href="./${item.engine}/fullscreen/${escapeHtml(fullscreenName)}">fullscreen</a> ${scoreHtml(item.score)}</li>`,
    );
  }

  writeFileSync(
    join(outputRoot, "index.html"),
    `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>v9 UI screenshot review</title>
  <style>${css()} main.review{height:auto;min-height:100vh;display:block}.topbar{margin-bottom:12px}.panes{display:block}.pane{margin-bottom:12px}</style>
</head>
<body>
  <main class="review">
    <header class="topbar">
      <div>
        <h1>v9 UI screenshot review</h1>
        <div class="meta">${escapeHtml(outputRoot)}</div>
      </div>
      <div class="score"><span>${cases.length} pages</span></div>
    </header>
    <section class="pane">
      <h2>Pages</h2>
      <div class="pane-body"><ul>${links.join("\n")}</ul></div>
    </section>
  </main>
</body>
</html>`,
    "utf8",
  );
  return cases;
}

function screenshotCases(outputRoot: string, cases: Case[]): void {
  const chrome =
    process.env.CHROME_BIN ||
    (existsSync("/usr/bin/google-chrome-stable")
      ? "/usr/bin/google-chrome-stable"
      : "google-chrome-stable");
  for (const item of cases) {
    const htmlName = `${slug(item.suite)}__${slug(item.file)}.html`;
    const fullscreenHtmlName = `${slug(item.suite)}__${slug(item.file)}.fullscreen.html`;
    const pngName = `${slug(item.suite)}__${slug(item.file)}.png`;
    const fullscreenPngName = `${slug(item.suite)}__${slug(item.file)}.fullscreen.png`;
    const htmlPath = join(outputRoot, item.engine, htmlName);
    const fullscreenHtmlPath = join(
      outputRoot,
      item.engine,
      "fullscreen",
      fullscreenHtmlName,
    );
    const enginePngPath = join(outputRoot, item.engine, pngName);
    const engineFullscreenPngPath = join(
      outputRoot,
      item.engine,
      "fullscreen",
      fullscreenPngName,
    );
    const allPngPath = join(outputRoot, "all", `${item.engine}__${pngName}`);
    const allFullscreenPngPath = join(
      outputRoot,
      "all",
      "fullscreen",
      `${item.engine}__${fullscreenPngName}`,
    );
    const profileDir = join(
      "/tmp",
      `ittm-v9-shot-${process.pid}-${item.engine}-${slug(item.file).slice(0, 32)}`,
    );
    captureChrome(
      chrome,
      profileDir,
      htmlPath,
      enginePngPath,
      allPngPath,
      `compare ${item.engine} ${item.file}`,
    );
    captureChrome(
      chrome,
      `${profileDir}-fullscreen`,
      fullscreenHtmlPath,
      engineFullscreenPngPath,
      allFullscreenPngPath,
      `fullscreen ${item.engine} ${item.file}`,
    );
  }
}

function captureChrome(
  chrome: string,
  profileDir: string,
  htmlPath: string,
  pngPath: string,
  copyPath: string,
  label: string,
): void {
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-component-update",
    "--no-first-run",
    `--user-data-dir=${profileDir}`,
    "--window-size=1920,1080",
    `--screenshot=${pngPath}`,
    fileUrl(htmlPath),
  ];
  try {
    execFileSync(chrome, args, { stdio: "ignore", timeout: 25_000 });
    writeFileSync(copyPath, readFileSync(pngPath));
    console.log(`shot ${label}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    writeFileSync(
      `${pngPath}.error.txt`,
      `${message}\nhtml=${htmlPath}\n`,
      "utf8",
    );
    console.error(`screenshot failed ${label}: ${message}`);
  }
}

const { outputRoot, suites, engines, fileFilters, allFiles, screenshots } =
  parseArgs();
const cases = collectCases(suites, engines, fileFilters, allFiles);
writeReviewPages(outputRoot, cases);
if (screenshots) {
  screenshotCases(outputRoot, cases);
}
console.log(`Wrote ${cases.length} review pages to ${outputRoot}`);
console.log(`Index: ${join(outputRoot, "index.html")}`);
console.log(`All screenshots: ${join(outputRoot, "all")}`);
for (const engine of ENGINES) {
  console.log(`${engine}: ${join(outputRoot, engine)}`);
}
