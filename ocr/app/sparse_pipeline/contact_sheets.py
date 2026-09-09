from __future__ import annotations

import base64
from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import TypeVar
from zipfile import ZipFile

from PIL import Image, ImageDraw

CONTACT_SHEET_ITEMS = 24
CONTACT_SHEET_COLUMNS = 3
CONTACT_SHEET_TILE_SIZE = (360, 210)
_ContactItem = TypeVar("_ContactItem")


def _write_paired_contact_sheets(
    root: Path,
    *,
    stem: str,
    first_label: str,
    second_label: str,
    items: tuple[_ContactItem, ...],
    load: Callable[[_ContactItem], tuple[str, Image.Image, Image.Image]],
    items_per_sheet: int = CONTACT_SHEET_ITEMS,
    columns: int = CONTACT_SHEET_COLUMNS,
) -> list[str]:
    if not items:
        return []
    sheet_root = root / "contact-sheets"
    sheet_root.mkdir(exist_ok=True)
    tile_width, tile_height = CONTACT_SHEET_TILE_SIZE
    rows = (items_per_sheet + columns - 1) // columns
    paths: list[str] = []
    for start in range(0, len(items), items_per_sheet):
        page_items = items[start : start + items_per_sheet]
        sheet = Image.new(
            "RGB",
            (tile_width * columns, tile_height * rows),
            "white",
        )
        try:
            draw = ImageDraw.Draw(sheet)
            for offset, item in enumerate(page_items):
                item_id, first, second = load(item)
                try:
                    column = offset % columns
                    row = offset // columns
                    left = column * tile_width
                    top = row * tile_height
                    draw.rectangle(
                        (
                            left,
                            top,
                            left + tile_width - 1,
                            top + tile_height - 1,
                        ),
                        outline=(190, 190, 190),
                    )
                    draw.text((left + 6, top + 5), item_id, fill=(0, 0, 0))
                    draw.text(
                        (left + 6, top + 21),
                        f"{first_label} | {second_label}",
                        fill=(70, 70, 70),
                    )
                    half_width = tile_width // 2
                    for side, preview in enumerate((first, second)):
                        preview.thumbnail(
                            (half_width - 14, tile_height - 52),
                            Image.Resampling.LANCZOS,
                        )
                        paste_left = left + side * half_width + (half_width - preview.width) // 2
                        paste_top = top + 43 + (tile_height - 48 - preview.height) // 2
                        sheet.paste(preview, (paste_left, paste_top))
                finally:
                    first.close()
                    second.close()
            stop = start + len(page_items) - 1
            path = sheet_root / f"{stem}-{start:06d}-{stop:06d}.png"
            sheet.save(path, format="PNG", compress_level=1)
        finally:
            sheet.close()
        paths.append(path.relative_to(root).as_posix())
    return paths


def write_paired_contact_sheets(
    root: Path,
    *,
    stem: str,
    first_label: str,
    second_label: str,
    items: tuple[tuple[str, Path, Path], ...],
) -> list[str]:
    """Render bounded pages of paired, actual crop files.

    The source files remain the primary evidence.  Contact sheets are only an
    index that lets a human spot a bad segment/block without opening thousands
    of PNG files one by one.
    """

    def load(item: tuple[str, Path, Path]) -> tuple[str, Image.Image, Image.Image]:
        item_id, first_path, second_path = item
        with Image.open(first_path) as opened:
            first = opened.convert("RGB")
        with Image.open(second_path) as opened:
            second = opened.convert("RGB")
        return item_id, first, second

    return _write_paired_contact_sheets(
        root,
        stem=stem,
        first_label=first_label,
        second_label=second_label,
        items=items,
        load=load,
    )


def write_paired_contact_sheets_from_archive(
    root: Path,
    *,
    archive_path: Path,
    stem: str,
    first_label: str,
    second_label: str,
    items: tuple[tuple[str, str, str], ...],
) -> list[str]:
    """Index archived pairs as lossless SVG without rerasterizing thumbnails."""

    if not items:
        return []
    sheet_root = root / "contact-sheets"
    sheet_root.mkdir(exist_ok=True)
    items_per_sheet = 96
    columns = 4
    tile_width, tile_height = CONTACT_SHEET_TILE_SIZE
    paths: list[str] = []
    with ZipFile(archive_path) as archive:
        for start in range(0, len(items), items_per_sheet):
            page_items = items[start : start + items_per_sheet]
            rows = (len(page_items) + columns - 1) // columns
            width = tile_width * columns
            height = tile_height * rows
            lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                (
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
                    f'height="{height}" viewBox="0 0 {width} {height}">'
                ),
                '<rect width="100%" height="100%" fill="white"/>',
            ]
            for offset, (item_id, first_member, second_member) in enumerate(page_items):
                column = offset % columns
                row = offset // columns
                left = column * tile_width
                top = row * tile_height
                lines.extend(
                    (
                        (
                            f'<rect x="{left}" y="{top}" width="{tile_width - 1}" '
                            f'height="{tile_height - 1}" fill="none" stroke="#bebebe"/>'
                        ),
                        (
                            f'<text x="{left + 6}" y="{top + 16}" '
                            f'font-family="monospace" font-size="12">'
                            f"{escape(item_id)}</text>"
                        ),
                        (
                            f'<text x="{left + 6}" y="{top + 34}" '
                            f'font-family="sans-serif" font-size="11" fill="#464646">'
                            f"{escape(first_label)} | {escape(second_label)}</text>"
                        ),
                    )
                )
                half_width = tile_width // 2
                for side, member in enumerate((first_member, second_member)):
                    encoded = base64.b64encode(archive.read(member)).decode("ascii")
                    x = left + side * half_width + 6
                    lines.append(
                        (
                            f'<image x="{x}" y="{top + 42}" '
                            f'width="{half_width - 12}" height="{tile_height - 48}" '
                            'preserveAspectRatio="xMidYMid meet" '
                            f'href="data:image/png;base64,{encoded}"/>'
                        )
                    )
            lines.append("</svg>")
            stop = start + len(page_items) - 1
            path = sheet_root / f"{stem}-{start:06d}-{stop:06d}.svg"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(path.relative_to(root).as_posix())
    return paths


__all__ = [
    "write_paired_contact_sheets",
    "write_paired_contact_sheets_from_archive",
]
