from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

CONTACT_SHEET_ITEMS = 24
CONTACT_SHEET_COLUMNS = 3
CONTACT_SHEET_TILE_SIZE = (360, 210)


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

    if not items:
        return []
    sheet_root = root / "contact-sheets"
    sheet_root.mkdir(exist_ok=True)
    tile_width, tile_height = CONTACT_SHEET_TILE_SIZE
    rows = (CONTACT_SHEET_ITEMS + CONTACT_SHEET_COLUMNS - 1) // (
        CONTACT_SHEET_COLUMNS
    )
    paths: list[str] = []
    for start in range(0, len(items), CONTACT_SHEET_ITEMS):
        page_items = items[start : start + CONTACT_SHEET_ITEMS]
        sheet = Image.new(
            "RGB",
            (tile_width * CONTACT_SHEET_COLUMNS, tile_height * rows),
            "white",
        )
        try:
            draw = ImageDraw.Draw(sheet)
            for offset, (item_id, first_path, second_path) in enumerate(
                page_items
            ):
                column = offset % CONTACT_SHEET_COLUMNS
                row = offset // CONTACT_SHEET_COLUMNS
                left = column * tile_width
                top = row * tile_height
                draw.rectangle(
                    (left, top, left + tile_width - 1, top + tile_height - 1),
                    outline=(190, 190, 190),
                )
                draw.text((left + 6, top + 5), item_id, fill=(0, 0, 0))
                draw.text(
                    (left + 6, top + 21),
                    f"{first_label} | {second_label}",
                    fill=(70, 70, 70),
                )
                half_width = tile_width // 2
                for side, source in enumerate((first_path, second_path)):
                    with Image.open(source) as opened:
                        preview = opened.convert("RGB")
                    try:
                        preview.thumbnail(
                            (half_width - 14, tile_height - 52),
                            Image.Resampling.LANCZOS,
                        )
                        paste_left = (
                            left
                            + side * half_width
                            + (half_width - preview.width) // 2
                        )
                        paste_top = top + 43 + (
                            tile_height - 48 - preview.height
                        ) // 2
                        sheet.paste(preview, (paste_left, paste_top))
                    finally:
                        preview.close()
            stop = start + len(page_items) - 1
            path = sheet_root / f"{stem}-{start:06d}-{stop:06d}.png"
            sheet.save(path, format="PNG")
        finally:
            sheet.close()
        paths.append(path.relative_to(root).as_posix())
    return paths


__all__ = ["write_paired_contact_sheets"]
