from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from app.chunking.vertical import (
    TableCell,
    TableLayout,
    prepare_table_cell_image,
    table_words_to_rows,
)
from app.recognition.contracts import TextOcrEngine


@dataclass
class _PackedCell:
    cell: TableCell
    image: Image.Image
    bbox: tuple[int, int, int, int]


def _is_numeric_text(text: str) -> bool:
    value = text.strip()
    return bool(value and re.search(r"\d", value) and re.fullmatch(r"[\d\s.,%+:/()\-]+", value))


def _seed_words_expect_numbers(seed_words: list[dict]) -> bool:
    texts = [str(word.get("text", "")).strip() for word in seed_words if str(word.get("text", "")).strip()]
    if not texts:
        return False
    numeric = sum(_is_numeric_text(text) for text in texts)
    return numeric > 0 and numeric / len(texts) >= 0.20


def _word_confidence(word: dict) -> float:
    try:
        return float(word.get("conf", 0))
    except (TypeError, ValueError):
        return 0.0


def _merge_numeric_cell_candidates(
    primary: list[dict],
    numeric: list[dict],
) -> list[dict]:
    merged = {tuple(word["bbox"]): word for word in primary}
    for word in numeric:
        if not _is_numeric_text(str(word.get("text", ""))):
            continue
        bbox = tuple(word["bbox"])
        current = merged.get(bbox)
        if current is None:
            merged[bbox] = word
            continue
        current_text = str(current.get("text", ""))
        if (
            not _is_numeric_text(current_text) and (len(current_text.strip()) <= 2 or _word_confidence(current) < 45)
        ) or (_is_numeric_text(current_text) and _word_confidence(word) > _word_confidence(current)):
            merged[bbox] = word
    return list(merged.values())


def recognize_missing_table_cell_batches(
    engine: TextOcrEngine,
    image: Image.Image,
    table: TableLayout,
    *,
    seed_words: list[dict] | None = None,
    max_batch_pixels: int = 8_000_000,
    target_width: int = 1800,
    gap: int = 24,
) -> tuple[list[dict], int]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words) or max_batch_pixels <= 0:
        return [], 0

    seed_rows = table_words_to_rows(table, seed_words or [])
    recognize_numeric_words = getattr(
        engine,
        "recognize_words_for_language",
        None,
    )
    numeric_retry = (
        callable(recognize_numeric_words) and table.cols >= 2 and _seed_words_expect_numbers(seed_words or [])
    )
    target_width = min(target_width, max_batch_pixels)
    max_batch_height = max(1, max_batch_pixels // max(1, target_width))
    if target_width <= gap * 2 or max_batch_height <= gap * 2:
        return [], 0
    recovered_words: list[dict] = []
    batch_calls = 0
    packed: list[_PackedCell] = []
    cursor_x = gap
    cursor_y = gap
    row_height = 0

    def flush() -> None:
        nonlocal batch_calls, packed
        if not packed:
            return
        sheet_height = min(
            max_batch_height,
            max(item.bbox[3] for item in packed) + gap,
        )
        sheet = Image.new("L", (target_width, sheet_height), "white")
        try:
            for item in packed:
                sheet.paste(item.image, (item.bbox[0], item.bbox[1]))
            words = recognize_words(sheet, psm=11, min_conf=5)
            batch_calls += 1
            mapped_words = _map_batch_words(words, packed)
            if numeric_retry:
                numeric_words = recognize_numeric_words(
                    sheet,
                    "equ",
                    psm=11,
                    min_conf=5,
                )
                batch_calls += 1
                mapped_words = _merge_numeric_cell_candidates(
                    mapped_words,
                    _map_batch_words(numeric_words, packed),
                )
            recovered_words.extend(mapped_words)
        finally:
            sheet.close()
            for item in packed:
                item.image.close()
            packed = []

    for cell in table.cells:
        if cell.is_empty:
            continue
        if seed_rows[cell.row][cell.col].strip():
            continue
        prepared = prepare_table_cell_image(
            image,
            cell.bbox,
            skip_blank=False,
        )
        if prepared is None:
            continue

        width, height = prepared.size
        usable_width = target_width - gap * 2
        usable_height = max_batch_height - gap * 2
        scale = min(
            1.0,
            usable_width / max(1, width),
            usable_height / max(1, height),
        )
        if scale < 1.0:
            resized = prepared.resize(
                (
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                ),
                getattr(Image, "Resampling", Image).LANCZOS,
            )
            prepared.close()
            prepared = resized
            width, height = prepared.size

        if cursor_x + width + gap > target_width:
            cursor_x = gap
            cursor_y += row_height + gap
            row_height = 0
        if packed and cursor_y + height + gap > max_batch_height:
            flush()
            cursor_x = gap
            cursor_y = gap
            row_height = 0

        packed.append(
            _PackedCell(
                cell=cell,
                image=prepared,
                bbox=(
                    cursor_x,
                    cursor_y,
                    cursor_x + width,
                    cursor_y + height,
                ),
            )
        )
        cursor_x += width + gap
        row_height = max(row_height, height)

    flush()
    return recovered_words, batch_calls


def _map_batch_words(
    words: list[dict],
    packed: list[_PackedCell],
) -> list[dict]:
    grouped: list[list[dict]] = [[] for _ in packed]
    for word in words:
        bbox = word.get("bbox")
        text = str(word.get("text", "")).strip()
        if not bbox or len(bbox) != 4 or not text:
            continue
        left, top, right, bottom = bbox
        x_center = (left + right) / 2
        y_center = (top + bottom) / 2
        for index, item in enumerate(packed):
            x1, y1, x2, y2 = item.bbox
            if x1 <= x_center <= x2 and y1 <= y_center <= y2:
                grouped[index].append(word)
                break

    recovered = []
    for item, cell_words in zip(packed, grouped):
        if not cell_words:
            continue
        cell_words.sort(
            key=lambda word: (
                word["bbox"][1],
                word["bbox"][0],
            )
        )
        text = " ".join(str(word.get("text", "")).strip() for word in cell_words if str(word.get("text", "")).strip())
        if not text:
            continue
        recovered.append(
            {
                "text": text,
                "bbox": item.cell.bbox,
                "conf": min(float(word.get("conf", 0)) for word in cell_words),
            }
        )
    return recovered
