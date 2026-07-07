#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path


class FirstTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.rows: list[list[str]] = []
        self.table_html_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and self.depth == 0:
            self.depth = 1
        elif self.depth:
            self.depth += 1

        if not self.depth:
            return

        self.table_html_parts.append(self.get_starttag_text() or "")
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = []
        elif tag == "br" and self.current_cell is not None:
            self.current_cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return

        if tag in {"td", "th"} and self.current_cell is not None:
            text = _clean_cell_text("".join(self.current_cell))
            if self.current_row is not None:
                self.current_row.append(text)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None

        self.table_html_parts.append(f"</{tag}>")
        if tag == "table" and self.depth == 1:
            self.depth = 0
        elif self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            return
        self.table_html_parts.append(html.escape(data))
        if self.current_cell is not None:
            self.current_cell.append(data)


def _clean_cell_text(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _source_text(source: str) -> tuple[str, str]:
    if ":" in source and not Path(source).exists():
        revision, path = source.split(":", 1)
        completed = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        return completed.stdout, source

    path = Path(source)
    return path.read_text(encoding="utf-8", errors="replace"), str(path)


def _first_table(markdown: str) -> tuple[str, list[list[str]]]:
    parser = FirstTableParser()
    parser.feed(markdown)
    if not parser.rows:
        raise ValueError("source contains no HTML table")
    return "".join(parser.table_html_parts), parser.rows


def _escape_markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _table_markdown(rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in padded[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def _render_html(table_html: str, *, title: str, header_text: str = "") -> str:
    caption_html = ""
    if header_text.strip():
        caption_html = (
            '<div class="ocr-table-caption">'
            f"{html.escape(header_text.strip())}"
            "</div>\n"
        )
        table_html = re.sub(
            r"<thead(\s[^>]*)?>",
            lambda match: f"<thead{match.group(1) or ''} style=\"display: none;\">",
            table_html,
            count=1,
        )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, Arial, sans-serif;
      background: #ffffff;
      color: #111827;
    }}
    html,
    body {{
      margin: 0;
      padding: 0;
      background: #ffffff;
      color: #111827;
    }}
    body {{
      width: max-content;
      min-width: 1800px;
      padding: 24px;
    }}
    .ocr-table-caption {{
      box-sizing: border-box;
      width: 1800px;
      margin: 0 0 10px;
      padding: 10px 12px;
      border: 2px solid #111827;
      background: #f3f4f6;
      color: #111827;
      font-size: 22px;
      font-weight: 700;
      line-height: 1.25;
    }}
    table {{
      width: 1800px !important;
      border-collapse: collapse;
      table-layout: fixed;
      background: #ffffff !important;
      color: #111827 !important;
      font-size: 18px;
      line-height: 1.38;
    }}
    th,
    td {{
      box-sizing: border-box;
      border: 2px solid #111827 !important;
      background: #ffffff !important;
      color: #111827 !important;
      padding: 10px 12px !important;
      overflow-wrap: anywhere;
      vertical-align: middle;
    }}
    th {{
      background: #f3f4f6 !important;
      color: #111827 !important;
      font-size: 20px;
      font-weight: 700;
    }}
    td:not([style*="background-color"]) {{
      background: #ffffff !important;
      color: #111827 !important;
    }}
  </style>
</head>
<body>
{caption_html}
{table_html}
</body>
</html>
"""


def _firefox_screenshot(html_path: Path, screenshot_path: Path) -> bool:
    firefox = shutil.which("firefox")
    if firefox is None:
        return False
    env = os.environ.copy()
    env.setdefault("MOZ_HEADLESS_WIDTH", "1880")
    env.setdefault("MOZ_HEADLESS_HEIGHT", "1800")
    subprocess.run(
        [
            firefox,
            "--headless",
            "--screenshot",
            str(screenshot_path.resolve()),
            html_path.resolve().as_uri(),
        ],
        check=True,
        env=env,
    )
    if not screenshot_path.exists():
        raise RuntimeError(f"Firefox did not write screenshot: {screenshot_path}")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render an HTML table from Markdown documentation and build the paired "
            "Markdown fixture reference."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Markdown path, or git object in REV:path form.",
    )
    parser.add_argument("--fixture", required=True, help="Fixture basename.")
    parser.add_argument("--fixtures-root", default=Path("debug/fixtures"), type=Path)
    parser.add_argument("--expected-root", default=Path("debug/reference"), type=Path)
    parser.add_argument(
        "--output-root", default=Path("debug/artifacts/doc-reference"), type=Path
    )
    parser.add_argument("--write-reference", action="store_true")
    parser.add_argument(
        "--publish-fixture",
        action="store_true",
        help="Copy the rendered screenshot into --fixtures-root/<fixture>.",
    )
    parser.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Only write extracted source/reference artifacts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_markdown, source_label = _source_text(args.source)
    table_html, rows = _first_table(source_markdown)

    fixture_dir = args.output_root / args.fixture
    fixture_dir.mkdir(parents=True, exist_ok=True)
    reference_markdown = _table_markdown(rows)
    html_path = fixture_dir / "render.html"
    reference_path = fixture_dir / "reference.md"
    source_path = fixture_dir / "source.md"
    manifest_path = fixture_dir / "manifest.md"
    screenshot_path = fixture_dir / "screenshot.png"

    source_path.write_text(source_markdown, encoding="utf-8")
    reference_path.write_text(reference_markdown, encoding="utf-8")
    html_path.write_text(
        _render_html(table_html, title=args.fixture, header_text=" ".join(rows[0])),
        encoding="utf-8",
    )

    screenshot_status = "skipped"
    if not args.no_screenshot:
        screenshot_status = "missing-firefox"
        if _firefox_screenshot(html_path, screenshot_path):
            screenshot_status = "written"

    written_reference = ""
    if args.write_reference:
        args.expected_root.mkdir(parents=True, exist_ok=True)
        target = args.expected_root / f"{args.fixture}.md"
        target.write_text(reference_markdown, encoding="utf-8")
        written_reference = str(target)

    written_fixture = ""
    if args.publish_fixture:
        if args.no_screenshot:
            raise ValueError("--publish-fixture requires screenshot generation")
        if not screenshot_path.exists():
            raise FileNotFoundError(f"Missing screenshot to publish: {screenshot_path}")
        args.fixtures_root.mkdir(parents=True, exist_ok=True)
        target = args.fixtures_root / args.fixture
        shutil.copyfile(screenshot_path, target)
        written_fixture = str(target)

    manifest_path.write_text(
        "\n".join(
            [
                "# Documentation Reference Fixture",
                "",
                f"- fixture: `{args.fixture}`",
                f"- source: `{source_label}`",
                f"- extracted rows: `{len(rows)}`",
                f"- html: `{html_path}`",
                f"- screenshot: `{screenshot_path}` ({screenshot_status})",
                f"- artifact reference: `{reference_path}`",
                f"- written fixture: `{written_fixture or 'none'}`",
                f"- written reference: `{written_reference or 'none'}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
