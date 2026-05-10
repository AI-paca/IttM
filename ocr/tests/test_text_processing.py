import asyncio

import pytest
from PIL import Image, ImageDraw

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    analyze_document_layout,
    detect_table_layouts,
    split_vertical,
    table_layout_to_markdown,
)
from app.formatting.markdown_formatter import MarkdownFormatter
from app.services import convert_service


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


def _table_fixture() -> Image.Image:
    image = Image.new("RGB", (420, 220), "white")
    draw = ImageDraw.Draw(image)

    for x in [20, 220, 400]:
        draw.line((x, 20, x, 200), fill="black", width=3)
    for y in [20, 110, 200]:
        draw.line((20, y, 400, y), fill="black", width=3)

    return image


def test_detect_table_layouts_finds_grid_cells():
    pytest.importorskip("cv2")
    layouts = detect_table_layouts(_table_fixture())

    assert len(layouts) == 1
    assert layouts[0].rows == 2
    assert layouts[0].cols == 2
    assert len(layouts[0].cells) == 4


def test_table_layout_to_markdown_ocr_cells_in_reading_order():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    texts = iter(["Предмет", "Часы", "Math", "42"])

    markdown = table_layout_to_markdown(_table_fixture(), layout, lambda _cell: next(texts))

    assert markdown == "| Предмет | Часы |\n| --- | --- |\n| Math | 42 |"


def test_analyze_document_layout_returns_isolated_table_region():
    pytest.importorskip("cv2")
    regions = analyze_document_layout(_table_fixture())

    assert len(regions) == 1
    assert regions[0].kind == "table"
    assert regions[0].table is not None
    assert regions[0].table.bbox[0] == 0
    assert regions[0].table.rows == 2


def test_convert_service_uses_table_layout_before_vertical_chunks(monkeypatch, tmp_path):
    pytest.importorskip("cv2")
    values = iter(["Предмет", "Часы", "Math", "42"])

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return next(values)

        def info(self):
            return {"engine": "fake"}

    monkeypatch.setattr(convert_service, "AutoEngine", lambda prefer_tesseract=True: FakeEngine())

    image_path = tmp_path / "table.png"
    _table_fixture().save(image_path)

    markdown, meta = asyncio.run(convert_service.convert(image_path, engine_type="auto"))

    assert "| Предмет | Часы |" in markdown
    assert "| Math | 42 |" in markdown
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 4
