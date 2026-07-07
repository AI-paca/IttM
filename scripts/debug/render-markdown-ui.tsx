import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarkdownContent } from "../../web/src/ui/MarkdownContent";
import { analyzeMarkdownGrammar } from "../../web/src/ui/markdown-diagnostics";

type Item = {
  label: string;
  path: string;
};

function usage(): never {
  console.error(
    [
      "Usage:",
      "  node --import tsx scripts/debug/render-markdown-ui.tsx --output-root DIR --item LABEL=PATH [--item LABEL=PATH ...]",
    ].join("\n"),
  );
  process.exit(2);
}

function parseArgs(): { outputRoot: string; items: Item[] } {
  const args = process.argv.slice(2);
  let outputRoot = "";
  const items: Item[] = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--output-root") {
      outputRoot = args[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (arg === "--item") {
      const raw = args[index + 1] ?? "";
      index += 1;
      const separator = raw.indexOf("=");
      if (separator <= 0) usage();
      items.push({
        label: raw.slice(0, separator),
        path: raw.slice(separator + 1),
      });
      continue;
    }
    usage();
  }
  if (!outputRoot || items.length === 0) usage();
  return { outputRoot: resolve(outputRoot), items };
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function readResultBody(path: string): string {
  const text = readFileSync(path, "utf8");
  const separator = "\n---\n";
  if (text.startsWith("# ") && text.includes(separator)) {
    return text.split(separator, 2)[1].trim();
  }
  return text.trim();
}

function slug(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9а-яА-ЯёЁ._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 180);
}

function css(): string {
  return `
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --surface: #fff;
      --border: #d9e2ec;
      --text: #16202a;
      --muted: #65758a;
      --thead: #eef3f8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(100% - 40px, 1760px);
      margin: 0 auto;
      padding: 28px 0 72px;
    }
    .meta {
      margin-bottom: 16px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    h1 {
      margin: 0 0 18px;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    article {
      min-height: 70vh;
      padding: 28px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 12px 32px rgb(15 23 42 / 0.08);
      font-size: 17px;
      line-height: 1.65;
      overflow-wrap: anywhere;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .toolbar button {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-size: 14px;
      font-weight: 700;
      padding: 8px 12px;
    }
    .toolbar button:hover {
      background: var(--thead);
    }
    .grammar-panel {
      display: none;
      margin-bottom: 14px;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      font-size: 14px;
    }
    .grammar-panel.is-open {
      display: block;
    }
    .grammar-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .grammar-metric {
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f8fafc;
    }
    .grammar-metric b {
      display: block;
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .grammar-metric span {
      display: block;
      margin-top: 2px;
      font-size: 18px;
      font-weight: 800;
    }
    .markdown-content { max-width: none; }
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
      font-size: 0.82em;
      line-height: 1.25;
    }
    th, td {
      padding: 8px 12px;
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
      padding: 16px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #f8fafc;
    }
  `;
}

function grammarHtml(markdown: string): string {
  const diagnostics = analyzeMarkdownGrammar(markdown);
  const messages = [...diagnostics.errors, ...diagnostics.warnings]
    .slice(0, 12)
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

function interactionScript(): string {
  return `
    const grammarButton = document.querySelector('[data-action="grammar"]');
    const fullscreenButton = document.querySelector('[data-action="fullscreen"]');
    const grammarPanel = document.querySelector('[data-role="grammar-panel"]');
    const result = document.querySelector('[data-role="result"]');
    grammarButton?.addEventListener('click', () => {
      grammarPanel?.classList.toggle('is-open');
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

function htmlFor(item: Item): string {
  const body = readResultBody(item.path);
  const rendered = renderToStaticMarkup(
    <MarkdownContent>{body}</MarkdownContent>,
  );
  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(item.label)}</title>
  <style>${css()}</style>
</head>
<body>
  <main>
    <div class="meta">${escapeHtml(item.path)}</div>
    <h1>${escapeHtml(item.label)}</h1>
    <div class="toolbar">
      <button type="button" data-action="grammar">Грамматика</button>
      <button type="button" data-action="fullscreen">Полный экран</button>
    </div>
    <section class="grammar-panel" data-role="grammar-panel">${grammarHtml(body)}</section>
    <article data-role="result">${rendered}</article>
  </main>
  <script>${interactionScript()}</script>
</body>
</html>
`;
}

const { outputRoot, items } = parseArgs();
mkdirSync(outputRoot, { recursive: true });
const links: string[] = [];
for (const item of items) {
  const outputName = `${slug(item.label)}.html`;
  writeFileSync(join(outputRoot, outputName), htmlFor(item), "utf8");
  links.push(
    `<li><a href="./${escapeHtml(outputName)}">${escapeHtml(item.label)}</a><br><code>${escapeHtml(item.path)}</code></li>`,
  );
}
writeFileSync(
  join(outputRoot, "index.html"),
  `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Markdown UI render index</title>
  <style>${css()}</style>
</head>
<body>
  <main>
    <h1>Markdown UI render index</h1>
    <article><ul>${links.join("\n")}</ul></article>
  </main>
</body>
</html>
`,
  "utf8",
);
console.log(`Wrote ${items.length + 1} HTML files to ${outputRoot}`);
console.log(`Index: ${join(outputRoot, "index.html")}`);
console.log(`Renderer: ${basename(import.meta.url)}`);
