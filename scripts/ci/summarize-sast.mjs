#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const reportPath = process.argv[2] || ".sast/semgrep.json";

function readReport(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    return {
      results: [],
      errors: [{ message: `Could not read ${filePath}: ${error.message}` }],
      paths: { scanned: [] },
    };
  }
}

function markdownTable(rows) {
  if (!rows.length) return "";
  const header =
    "| Severity | Rule | Location | Message |\n| --- | --- | --- | --- |";
  return [
    header,
    ...rows.map((row) => {
      const location = `${row.path}:${row.start?.line ?? "?"}`;
      const message = String(row.extra?.message || "").replace(/\s+/g, " ");
      const severity = row.extra?.severity || "UNKNOWN";
      const rule = row.check_id || "unknown";
      return `| ${severity} | \`${rule}\` | \`${location}\` | ${message} |`;
    }),
  ].join("\n");
}

const report = readReport(reportPath);
const results = Array.isArray(report.results) ? report.results : [];
const errors = Array.isArray(report.errors) ? report.errors : [];
const scanned = Array.isArray(report.paths?.scanned)
  ? report.paths.scanned
  : [];
const grouped = results.reduce((acc, result) => {
  const severity = result.extra?.severity || "UNKNOWN";
  acc[severity] = (acc[severity] || 0) + 1;
  return acc;
}, {});

const lines = [
  "## SAST Summary",
  "",
  `Report: \`${reportPath}\``,
  `Scanned paths: ${scanned.length || "unknown"}`,
  `Findings: ${results.length}`,
];

if (Object.keys(grouped).length) {
  lines.push(
    `By severity: ${Object.entries(grouped)
      .map(([severity, count]) => `${severity}=${count}`)
      .join(", ")}`,
  );
}

if (errors.length) {
  lines.push("", "### Scanner Errors", "");
  for (const error of errors.slice(0, 10)) {
    lines.push(`- ${error.message || JSON.stringify(error)}`);
  }
}

if (results.length) {
  lines.push("", "### Findings", "", markdownTable(results.slice(0, 25)));
  if (results.length > 25) {
    lines.push("", `Showing first 25 of ${results.length} findings.`);
  }
} else {
  lines.push("", "No Semgrep findings for the configured SAST rules.");
}

const summary = `${lines.join("\n")}\n`;
process.stdout.write(summary);

if (process.env.GITHUB_STEP_SUMMARY) {
  fs.mkdirSync(path.dirname(process.env.GITHUB_STEP_SUMMARY), {
    recursive: true,
  });
  fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
}
