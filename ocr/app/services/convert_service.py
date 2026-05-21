from pathlib import Path
from typing import Tuple

from PIL import Image

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    analyze_document_layout,
    erase_table_lines_for_ocr,
    logical_table_layout,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_markdown,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)
from app.engines.auto_engine import AutoEngine
from app.formatting.markdown_formatter import MarkdownFormatter

# Disable maximum image pixel limit for long screenshots
Image.MAX_IMAGE_PIXELS = None


def _format_card_to_markdown(card_text: str, card_index: int) -> str:
    """
    Formats a single chunk OCR result to Markdown.
    """
    return card_text.strip()


def _recognize_image_region(engine, image: Image.Image) -> Tuple[list[str], int, int]:
    width, height = image.size
    if height <= 1600 or (width > 0 and height / width <= 1.8):
        return [engine.recognize(image, mode="text_mode")], 1, 0

    cards = split_by_blank_bands(image)
    if cards and len(cards) > 1:
        page_parts = []
        cards_found = 0
        for i, card_img in enumerate(cards):
            if card_img.size[1] < 50:
                continue
            cards_found += 1
            card_text = engine.recognize(card_img, mode="text_mode")
            page_parts.append(_format_card_to_markdown(card_text, i))
        return page_parts, cards_found, cards_found

    chunks = split_vertical(image, chunk_height=1200, overlap=100)
    page_texts = [engine.recognize(chunk, mode="text_mode") for chunk in chunks]
    return ["\n\n".join(dedupe_chunks(page_texts))], len(chunks), 0


def _recognize_table_cell(engine, image: Image.Image) -> str:
    width, height = image.size
    psm = 7 if height <= 80 or width <= 180 else 6
    return engine.recognize(image, mode="text_mode", psm=psm)


def _line_bounded_segments(lines: tuple[int, ...], limit: int, max_span: int) -> list[tuple[int, int]]:
    if len(lines) < 2:
        return [(0, limit)]

    normalized = sorted({max(0, min(limit, line)) for line in lines})
    if normalized[0] > 0:
        normalized.insert(0, 0)
    if normalized[-1] < limit:
        normalized.append(limit)

    segments = []
    start_index = 0
    while start_index < len(normalized) - 1:
        end_index = start_index + 1
        while end_index + 1 < len(normalized) and normalized[end_index + 1] - normalized[start_index] <= max_span:
            end_index += 1

        if normalized[end_index] <= normalized[start_index]:
            end_index = start_index + 1

        segments.append((normalized[start_index], normalized[end_index]))
        start_index = end_index

    return segments


def _recognize_scaled_words(
    engine, image: Image.Image, *, scale: int = 3, psm: int = 6, min_conf: int = 0
) -> list[dict]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return []

    resample = getattr(Image, "Resampling", Image).LANCZOS
    scaled = image.resize((max(1, image.size[0] * scale), max(1, image.size[1] * scale)), resample)
    words = recognize_words(erase_table_lines_for_ocr(scaled), psm=psm, min_conf=min_conf)
    scaled_words = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        left, top, right, bottom = bbox
        scaled_words.append(
            {
                **word,
                "bbox": (
                    int(round(left / scale)),
                    int(round(top / scale)),
                    int(round(right / scale)),
                    int(round(bottom / scale)),
                ),
            }
        )
    return scaled_words


def _merge_wide_table_left_strip_words(engine, image: Image.Image, table, words: list[dict]) -> list[dict]:
    if table.cols < 45 or len(table.x_lines) < 3:
        return words

    left_limit = min(image.size[0], max(1, int(table.x_lines[2])))
    if left_limit <= 1:
        return words

    left_words = _recognize_scaled_words(
        engine, image.crop((0, 0, left_limit, image.size[1])), scale=3, psm=6, min_conf=0
    )
    if len(left_words) < 10:
        return words

    remaining = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        left, _, right, _ = bbox
        if (left + right) / 2 > left_limit:
            remaining.append(word)

    return [*remaining, *left_words]


def _recognize_table_words(engine, image: Image.Image, table, *, single_pass: bool = False) -> list[dict]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return []

    width, height = image.size
    if single_pass or (table.cols <= 4 and height <= 3600):
        words = recognize_words(erase_table_lines_for_ocr(image), psm=6, min_conf=18)
        if single_pass:
            return _merge_wide_table_left_strip_words(engine, image, table, words)
        return words

    x_segments = _line_bounded_segments(table.x_lines, width, max_span=1700)
    y_segments = _line_bounded_segments(table.y_lines, height, max_span=1300)
    psm = 11 if len(table.cells) > 200 else 6
    min_conf = 18 if len(table.cells) <= 200 else 25

    words = []
    for y1, y2 in y_segments:
        for x1, x2 in x_segments:
            tile = image.crop((x1, y1, x2, y2))
            tile_words = recognize_words(erase_table_lines_for_ocr(tile), psm=psm, min_conf=min_conf)
            for word in tile_words:
                bbox = word.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                bx1, by1, bx2, by2 = bbox
                words.append(
                    {
                        **word,
                        "bbox": (bx1 + x1, by1 + y1, bx2 + x1, by2 + y1),
                    }
                )

    return words


async def convert(path: Path, engine_type: str = "auto") -> Tuple[str, dict]:
    """
    Convert document to markdown using specified OCR engine.

    Args:
        path: Path to the document (image or PDF)
        engine_type: 'auto' (Tesseract first), 'tesseract' (core), or 'easyocr' (high-quality)
    """
    # 1. Load document (image or pdf)
    images = []
    if path.suffix.lower() == ".pdf":
        try:
            from pdf2image import convert_from_path

            images = convert_from_path(str(path), dpi=300, fmt="png")
        except Exception as e:
            raise ValueError(f"Failed to process PDF: {str(e)}")
    else:
        try:
            img = Image.open(path)
            img.load()  # verify image works
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # Apply white background for transparent images
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                images = [bg]
            else:
                images = [img.convert("RGB")]
        except Exception as e:
            raise ValueError(f"Could not load image: {str(e)}")

    if not images:
        raise ValueError("Could not load image or parsed zero pages.")

    # Initialize engine based on type
    if engine_type == "tesseract":
        from app.engines.tesseract_engine import TesseractEngine

        engine = TesseractEngine()
    elif engine_type == "easyocr":
        from app.engines.easyocr_engine import EasyOcrEngine

        engine = EasyOcrEngine()
        if not engine.available():
            raise ValueError(f"EasyOCR is not installed or initialization failed: {engine.info().get('init_error')}")
    else:  # auto
        engine = AutoEngine(prefer_tesseract=True)

    # We will simply process all pages and merge
    all_markdown_parts = []
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0

    for main_image in images:
        page_parts = []
        regions = analyze_document_layout(main_image)

        for region in regions:
            if region.kind == "table" and region.table is not None:
                is_wide_table = region.table.cols >= 45
                table_layout = region.table if is_wide_table else logical_table_layout(region.image, region.table)
                tables_found += 1
                table_cells += len(table_layout.cells)
                total_chunks += len(table_layout.cells)

                table_md = ""
                table_words = _recognize_table_words(engine, region.image, table_layout, single_pass=is_wide_table)
                if table_words:
                    table_md = (
                        wide_curriculum_table_to_markdown(table_layout, table_words)
                        if is_wide_table
                        else table_words_to_markdown(table_layout, table_words)
                    )

                if not table_md.strip():
                    table_md = table_layout_to_markdown(
                        region.image,
                        table_layout,
                        lambda cell_image: _recognize_table_cell(engine, cell_image),
                    )
                if table_md.strip():
                    page_parts.append(table_md)
                continue

            region_parts, region_chunks, region_cards = _recognize_image_region(engine, region.image)
            total_chunks += region_chunks
            cards_found += region_cards
            page_parts.extend(part for part in region_parts if part.strip())

        all_markdown_parts.append("\n\n".join(page_parts))

    merged_text = "\n\n---\n\n".join(all_markdown_parts)

    # 5. Format as Markdown
    markdown = MarkdownFormatter.format_text(merged_text)

    meta = {
        "engine": engine.info()["engine"],
        "chunks": total_chunks,
        "cards_found": cards_found,
        "tables_found": tables_found,
        "table_cells": table_cells,
        "pages": len(images),
        "elapsed_ms": 0,  # to be overwritten in router
    }

    return markdown, meta
