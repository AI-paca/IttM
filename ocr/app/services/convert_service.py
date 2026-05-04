from pathlib import Path
from typing import Tuple
from PIL import Image
from app.chunking.vertical import split_vertical, split_by_blank_bands
from app.chunking.dedupe import dedupe_chunks
from app.formatting.markdown_formatter import MarkdownFormatter
from app.engines.tesseract_engine import TesseractEngine


def _format_card_to_markdown(card_result: dict, card_index: int) -> str:
    """
    Formats a single card OCR result to Markdown.
    """
    lines = []
    lines.append(f"### Товар {card_index + 1}")
    
    if card_result.get("price"):
        lines.append(f"**Цена:** {card_result['price']}")
    
    if card_result.get("quantity"):
        lines.append(f"**Количество:** {card_result['quantity']}")
    
    if card_result.get("full_text"):
        lines.append("\n**Описание:**")
        lines.append(card_result['full_text'])
    
    return "\n".join(lines)


async def convert(path: Path) -> Tuple[str, dict]:
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
            images = [Image.open(path)]
            images[0].load()  # verify image works
        except Exception as e:
            raise ValueError(f"Could not load image: {str(e)}")

    if not images:
        raise ValueError("Could not load image or parsed zero pages.")

    # We will simply process all pages and merge
    all_markdown_parts = []
    engine = TesseractEngine()
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
                
                # Use card-aware OCR
                card_result = engine.recognize_card(card_img)
                
                # Format this card as Markdown
                card_md = _format_card_to_markdown(card_result, i)
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
                # Determine if chunk looks like a card (aspect ratio check)
                w, h = chunk.size
                if h > w:  # Tall chunk - likely a card
                    card_result = engine.recognize_card(chunk)
                    page_texts.append(card_result.get("full_text", ""))
                else:
                    # Regular text block
                    page_texts.append(engine.recognize(chunk, mode="text_mode", psm=6))
            
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
