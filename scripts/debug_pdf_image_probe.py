#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image

DEFAULT_FORMATS = ("png", "jpg")


def _render_pdf(path: Path, *, dpi: int, max_pages: int) -> list[Image.Image]:
    from pdf2image import convert_from_path

    return convert_from_path(
        str(path),
        dpi=dpi,
        first_page=1,
        last_page=max_pages,
        fmt="png",
    )


def _stack_pages(pages: list[Image.Image], *, gap: int) -> Image.Image:
    if not pages:
        raise ValueError("PDF rendered no pages")

    rgb_pages: list[Image.Image] = []
    try:
        rgb_pages = [page.convert("RGB") for page in pages]
        width = max(page.width for page in rgb_pages)
        height = sum(page.height for page in rgb_pages) + gap * (len(rgb_pages) - 1)
        stacked = Image.new("RGB", (width, height), "white")

        offset = 0
        for page in rgb_pages:
            left = (width - page.width) // 2
            stacked.paste(page, (left, offset))
            offset += page.height + gap
        return stacked
    finally:
        for page in rgb_pages:
            page.close()


def _limited_expected_text(text: str, *, max_pages: int) -> str:
    lines = text.splitlines(keepends=True)
    page_heading_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"#{1,6}\s+page\s+\d+", line.strip(), re.I)
    ]
    if page_heading_indexes:
        if len(page_heading_indexes) > max_pages:
            return "".join(lines[: page_heading_indexes[max_pages]]).rstrip() + "\n"
        return text.rstrip() + "\n"

    form_feed_pages = text.split("\f")
    if len(form_feed_pages) > 1:
        return "\f".join(form_feed_pages[:max_pages]).rstrip() + "\n"

    return text.rstrip() + "\n"


def _page_expected_texts(
    text: str,
    *,
    max_pages: int,
    rendered_pages: int,
) -> list[str]:
    lines = text.splitlines(keepends=True)
    page_heading_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"#{1,6}\s+page\s+\d+", line.strip(), re.I)
    ]
    if page_heading_indexes:
        pages: list[str] = []
        for page_index, start in enumerate(page_heading_indexes[:max_pages]):
            end = (
                page_heading_indexes[page_index + 1]
                if page_index + 1 < len(page_heading_indexes)
                else len(lines)
            )
            pages.append("".join(lines[start:end]).rstrip() + "\n")
        return pages

    form_feed_pages = text.split("\f")
    if len(form_feed_pages) > 1:
        return [page.rstrip() + "\n" for page in form_feed_pages[:max_pages]]

    if rendered_pages == 1:
        return [text.rstrip() + "\n"]
    return []


def _normalized_formats(formats: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in formats:
        for item in value.split(","):
            item = item.strip().lower()
            if item == "jpeg":
                item = "jpg"
            if item not in {"png", "jpg"}:
                raise ValueError(f"Unsupported raster format: {item}")
            if item not in normalized:
                normalized.append(item)
    if not normalized:
        raise ValueError("At least one raster format is required")
    return tuple(normalized)


def rasterize_pdf(
    source: Path,
    *,
    expected_root: Path,
    output_dir: Path,
    probe_reference_root: Path,
    dpi: int,
    max_pages: int,
    gap: int,
    stack_pages: bool = False,
    formats: tuple[str, ...] = DEFAULT_FORMATS,
) -> list[Path]:
    expected = expected_root / f"{source.name}.md"
    if not expected.exists():
        raise FileNotFoundError(f"Missing expected file: {expected}")
    expected_text = expected.read_text(encoding="utf-8", errors="replace")

    pages = _render_pdf(source, dpi=dpi, max_pages=max_pages)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe_reference_root.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        if stack_pages:
            limited_expected = _limited_expected_text(
                expected_text,
                max_pages=max_pages,
            )
            image = _stack_pages(pages, gap=gap)
            try:
                for image_format in formats:
                    suffix = "jpg" if image_format == "jpg" else "png"
                    output = output_dir / f"{source.name}.raster.{suffix}"
                    if image_format == "jpg":
                        image.save(output, format="JPEG", quality=92, optimize=True)
                    else:
                        image.save(output, format="PNG")
                    (probe_reference_root / f"{output.name}.md").write_text(
                        limited_expected,
                        encoding="utf-8",
                    )
                    outputs.append(output)
            finally:
                image.close()
            return outputs

        expected_pages = _page_expected_texts(
            expected_text,
            max_pages=max_pages,
            rendered_pages=len(pages),
        )

        for page_number, page in enumerate(pages, start=1):
            image = page.convert("RGB")
            try:
                for image_format in formats:
                    suffix = "jpg" if image_format == "jpg" else "png"
                    output = (
                        output_dir
                        / f"{source.name}.page-{page_number:03d}.raster.{suffix}"
                    )
                    if image_format == "jpg":
                        image.save(output, format="JPEG", quality=92, optimize=True)
                    else:
                        image.save(output, format="PNG")
                    if page_number <= len(expected_pages):
                        (probe_reference_root / f"{output.name}.md").write_text(
                            expected_pages[page_number - 1],
                            encoding="utf-8",
                        )
                    outputs.append(output)
            finally:
                image.close()
        return outputs
    finally:
        for page in pages:
            page.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rasterize PDF debug fixtures into page image files for OCR image-path probes."
    )
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--expected-root", default=Path("debug/reference"), type=Path)
    parser.add_argument(
        "--output-dir", default=Path("debug/tmp/pdf-image-fixtures"), type=Path
    )
    parser.add_argument(
        "--probe-reference-root",
        default=Path("debug/tmp/pdf-image-reference"),
        type=Path,
    )
    parser.add_argument("--dpi", default=300, type=int)
    parser.add_argument("--max-pages", default=5, type=int)
    parser.add_argument("--gap", default=32, type=int)
    parser.add_argument(
        "--stack-pages",
        action="store_true",
        help="Write one tall image per PDF instead of one raster image per page.",
    )
    parser.add_argument(
        "--format",
        action="append",
        default=[],
        dest="formats",
        help="Raster output format: png, jpg, jpeg, or comma-separated values. May be repeated.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    formats = _normalized_formats(args.formats or list(DEFAULT_FORMATS))
    for source in args.pdfs:
        outputs = rasterize_pdf(
            source,
            expected_root=args.expected_root,
            output_dir=args.output_dir,
            probe_reference_root=args.probe_reference_root,
            dpi=args.dpi,
            max_pages=args.max_pages,
            gap=args.gap,
            stack_pages=args.stack_pages,
            formats=formats,
        )
        for output in outputs:
            print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
