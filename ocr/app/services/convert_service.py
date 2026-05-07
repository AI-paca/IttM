from pathlib import Path
from typing import Tuple
from PIL import Image

# Disable maximum image pixel limit for long screenshots
Image.MAX_IMAGE_PIXELS = None

from app.chunking.vertical import split_vertical, split_by_blank_bands
from app.chunking.dedupe import dedupe_chunks
from app.formatting.markdown_formatter import MarkdownFormatter
from app.engines.auto_engine import AutoEngine


def _format_card_to_markdown(card_text: str, card_index: int) -> str:
    """
    Formats a single chunk OCR result to Markdown.
    """
    return card_text.strip()


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
            images = convert_from_path(str(path))
        except Exception as e:
            raise ValueError(f"Failed to process PDF: {str(e)}")
    else:
        try:
            img = Image.open(path)
            img.load()  # verify image works
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
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
    
    for page_num, main_image in enumerate(images):
        # 2. Try card-aware chunking first (split by blank bands between cards)
        cards = split_by_blank_bands(main_image)
        
        if cards and len(cards) > 1:
            # We found distinct cards
            cards_found += len(cards)
            total_chunks += len(cards)
            
            page_parts = []
            for i, card_img in enumerate(cards):
                # Skip very small chunks (likely noise)
                if card_img.size[1] < 50:
                    continue
                
                # Use OCR to recognize card
                card_text = engine.recognize(card_img, mode="text_mode")
                
                # Format this card as Markdown
                card_md = _format_card_to_markdown(card_text, i)
                page_parts.append(card_md)
            
            all_markdown_parts.append("\n\n".join(page_parts))
        else:
            # Fallback: no clear card boundaries found
            # Use regular chunking with overlap
            chunks = split_vertical(main_image, chunk_height=1200, overlap=100)
            total_chunks += len(chunks)
            
            # Recognize each chunk
            page_texts = []
            for chunk in chunks:
                # Recognize text using OCR
                text = engine.recognize(chunk, mode="text_mode")
                page_texts.append(text)
            
            # Dedupe overlaps
            clean_texts = dedupe_chunks(page_texts)
            all_markdown_parts.append("\n\n".join(clean_texts))
    
    merged_text = "\n\n---\n\n".join(all_markdown_parts)
    
    # 5. Format as Markdown
    markdown = MarkdownFormatter.format_text(merged_text)
    
    meta = {
        "engine": engine.info()["engine"],
        "chunks": total_chunks,
        "cards_found": cards_found,
        "pages": len(images),
        "elapsed_ms": 0  # to be overwritten in router
    }
    
    return markdown, meta
