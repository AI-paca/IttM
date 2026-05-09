from PIL import Image, ImageDraw

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import split_vertical
from app.formatting.markdown_formatter import MarkdownFormatter


def test_dedupe_chunks_removes_exact_normalized_duplicates():
    chunks = ["Hello   world", "Hello world", "Unique line"]

    assert dedupe_chunks(chunks) == ["Hello   world", "Unique line"]


def test_markdown_formatter_normalizes_bullets_and_whitespace():
    formatted = MarkdownFormatter.format_text("  • item one\n\n\n– item two\nplain  ")

    assert formatted == "- item one\n\n- item two\nplain"


def test_split_vertical_returns_at_least_one_chunk_for_small_image():
    image = Image.new("RGB", (300, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 80), "Hello OCR", fill="black")

    chunks = split_vertical(image, chunk_height=400, overlap=50)

    assert len(chunks) == 1
    assert chunks[0].size[0] <= image.size[0]
