#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import markdown


DEFAULT_DOCS = (
    "README.md",
    "docs/ru/architecture.md",
    "docs/ru/architecture-current-flags.md",
    "docs/ru/architecture-unified-pipeline.md",
    "docs/ru/testing.md",
    "docs/ru/security.md",
    "docs/ru/engine/README.md",
)


class VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "details",
        "div",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._row_depth = 0
        self._cell_depth = 0
        self._line_prefix: str | None = None
        self._details_stack: list[bool] = []
        self._summary_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "details":
            self._details_stack.append(any(name == "open" for name, _ in attrs))
            self._flush()
            return
        if tag == "summary":
            self._summary_depth += 1
            self._flush()
            return
        if self._hidden_by_closed_details():
            return
        if tag == "br":
            self._flush()
            return
        if tag == "pre":
            self._pre_depth += 1
            self._flush()
            return
        if tag == "tr":
            self._row_depth += 1
            self._flush()
            return
        if tag in {"td", "th"}:
            self._cell_depth += 1
            if self._current:
                self._current.append(" | ")
            return
        if tag == "li":
            self._flush()
            self._line_prefix = "- "
            return
        if tag in self.BLOCK_TAGS:
            self._flush()
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "summary":
            self._flush()
            self._summary_depth = max(0, self._summary_depth - 1)
            return
        if tag == "details":
            if not self._hidden_by_closed_details():
                self._flush()
            if self._details_stack:
                self._details_stack.pop()
            return
        if self._hidden_by_closed_details():
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._flush()
            return
        if tag == "tr":
            self._row_depth = max(0, self._row_depth - 1)
            self._flush()
            return
        if tag in {"td", "th"}:
            self._cell_depth = max(0, self._cell_depth - 1)
            return
        if tag == "li":
            self._flush()
            self._line_prefix = None
            return
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._hidden_by_closed_details():
            return
        self._append_text(data)

    def _append_text(self, value: str) -> None:
        if self._pre_depth:
            for index, line in enumerate(value.splitlines()):
                if index:
                    self._flush()
                if line.strip():
                    self._current.append(line.rstrip())
            return
        normalized = re.sub(r"\s+", " ", value)
        if normalized.strip():
            if self._current and not self._current[-1].endswith((" ", "\n", "| ")):
                self._current.append(" ")
            self._current.append(normalized.strip())

    def _flush(self) -> None:
        line = "".join(self._current).strip()
        self._current = []
        if not line:
            return
        if self._line_prefix and not line.startswith(self._line_prefix):
            line = f"{self._line_prefix}{line}"
        line = re.sub(r"\s+\|\s+", " | ", line)
        self.lines.append(line)

    def _hidden_by_closed_details(self) -> bool:
        return any(not is_open for is_open in self._details_stack) and not self._summary_depth

    def close(self) -> None:
        super().close()
        self._flush()


def _slug(path: Path) -> str:
    value = path.as_posix()
    value = value.removesuffix("/README.md").removesuffix(".md")
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", value).strip("_")
    return f"doc_{value}.png"


def _markdown_body(markdown_text: str) -> str:
    return markdown.markdown(
        markdown_text,
        extensions=("tables", "fenced_code", "sane_lists", "toc"),
        output_format="html5",
    )


def _reference_text(rendered_html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(rendered_html)
    parser.close()
    lines = [
        line for line in parser.lines
        if not _mostly_line_art(line)
    ]
    return "\n".join(lines).rstrip() + "\n"


def _mostly_line_art(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if not compact:
        return False
    line_art = set("┌┐└┘├┤┬┴┼─│━┃┏┓┗┛╭╮╰╯╔╗╚╝═║╠╣╦╩╬▼▲")
    line_art_count = sum(1 for char in compact if char in line_art)
    if line_art_count and not any(char.isalnum() for char in compact):
        return True
    return line_art_count >= 4 and line_art_count / len(compact) >= 0.35


def _html(body: str, *, title: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, Arial, sans-serif;
      background: white;
      color: #202124;
    }}
    body {{
      box-sizing: border-box;
      width: 1100px;
      margin: 0;
      padding: 34px 46px;
      background: white;
      color: #202124;
      font-size: 21px;
      line-height: 1.48;
    }}
    h1, h2, h3 {{
      margin: 1.25em 0 0.45em;
      line-height: 1.15;
    }}
    h1:first-child {{
      margin-top: 0;
    }}
    p, ul, ol, pre, table {{
      margin: 0.7em 0;
    }}
    code, pre {{
      font-family: "DejaVu Sans Mono", Consolas, monospace;
      font-size: 0.9em;
    }}
    pre {{
      padding: 12px;
      overflow: hidden;
      border: 1px solid #d0d7de;
      background: #f6f8fa;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    th, td {{
      border: 1px solid #d0d7de;
      padding: 10px 12px;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #f6f8fa;
      font-weight: 700;
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _firefox_screenshot(html_path: Path, screenshot_path: Path) -> None:
    firefox = shutil.which("firefox")
    if firefox is None:
        raise RuntimeError("firefox is required for browser documentation screenshots")
    env = os.environ.copy()
    env.setdefault("MOZ_HEADLESS_WIDTH", "1184")
    env.setdefault("MOZ_HEADLESS_HEIGHT", "12000")
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render project Markdown documentation as browser PNG fixtures."
    )
    parser.add_argument("--doc", action="append", default=[], type=Path)
    parser.add_argument("--fixtures-root", default=Path("debug/fixtures"), type=Path)
    parser.add_argument("--expected-root", default=Path("debug/reference"), type=Path)
    parser.add_argument(
        "--output-root", default=Path("debug/artifacts/doc-fixtures"), type=Path
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    docs = args.doc or [Path(path) for path in DEFAULT_DOCS]
    args.fixtures_root.mkdir(parents=True, exist_ok=True)
    args.expected_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[Path, Path, Path]] = []
    for source in docs:
        if not source.exists():
            raise FileNotFoundError(source)
        source_text = source.read_text(encoding="utf-8", errors="replace")
        fixture_name = _slug(source)
        artifact_dir = args.output_root / fixture_name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        html_path = artifact_dir / "render.html"
        screenshot_path = artifact_dir / fixture_name
        reference_path = artifact_dir / f"{fixture_name}.md"
        fixture_target = args.fixtures_root / fixture_name
        reference_target = args.expected_root / f"{fixture_name}.md"

        body = _markdown_body(source_text)
        html_path.write_text(_html(body, title=source.as_posix()), encoding="utf-8")
        reference_text = _reference_text(body)
        reference_path.write_text(reference_text, encoding="utf-8")
        _firefox_screenshot(html_path, screenshot_path)
        shutil.copyfile(screenshot_path, fixture_target)
        shutil.copyfile(reference_path, reference_target)
        rows.append((source, fixture_target, reference_target))

    manifest = args.output_root / "manifest.md"
    lines = [
        "# Markdown Documentation Fixtures",
        "",
        "| Source | Fixture | Reference |",
        "| --- | --- | --- |",
    ]
    for source, fixture, reference in rows:
        lines.append(f"| `{source}` | `{fixture}` | `{reference}` |")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
