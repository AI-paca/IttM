from pathlib import Path
from typing import Tuple

from PIL import Image

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    analyze_document_layout,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_markdown,
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
                tables_found += 1
                table_cells += len(region.table.cells)
                total_chunks += len(region.table.cells)

                table_md = table_layout_to_markdown(
                    region.image,
                    region.table,
                    lambda cell_image: engine.recognize(cell_image, mode="text_mode"),
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
