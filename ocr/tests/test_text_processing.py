import asyncio

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    TableLayout,
    analyze_document_layout,
    detect_table_layouts,
    logical_table_layout,
    split_vertical,
    table_layout_to_markdown,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
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

    for x, y in [(60, 58), (260, 58), (60, 148), (260, 148)]:
        draw.text((x, y), "x", fill="black")

    return image


def _noisy_table_fixture() -> Image.Image:
    rng = np.random.default_rng(42)
    base = np.full((240, 460, 3), 244, dtype=np.uint8)
    noise = rng.integers(0, 18, size=base.shape, dtype=np.uint8)
    base = np.clip(base - noise, 0, 255).astype(np.uint8)
    image = Image.fromarray(base).convert("RGB")
    draw = ImageDraw.Draw(image)

    for x in [25, 180, 315, 435]:
        draw.line((x, 30, x, 215), fill="black", width=2)
    for y in [30, 88, 148, 215]:
        draw.line((25, y, 435, y), fill="black", width=2)
    draw.text((45, 52), "Subject", fill="black")
    draw.text((205, 52), "Hours", fill="black")
    draw.text((335, 52), "Code", fill="black")
    draw.text((45, 112), "Math", fill="black")
    draw.text((205, 112), "42", fill="black")

    return image


def _hierarchical_indent_table_fixture() -> Image.Image:
    image = Image.new("RGB", (560, 230), "white")
    draw = ImageDraw.Draw(image)

    for x in [20, 160, 320, 540]:
        draw.line((x, 20, x, 210), fill="black", width=3)
    for y in [20, 60, 100, 140, 180, 210]:
        draw.line((20, y, 540, y), fill="black", width=3)

    # Short hierarchy stroke inside the first logical column. It should not
    # become its own Markdown column.
    draw.line((70, 100, 70, 210), fill="black", width=3)

    draw.text((60, 34), "Index", fill="black")
    draw.text((210, 34), "Name", fill="black")
    draw.text((390, 34), "Competencies", fill="black")
    draw.text((35, 113), "B1.O.01", fill="black")
    draw.text((180, 113), "Math", fill="black")
    draw.text((340, 113), "UK-1", fill="black")

    return image


def test_detect_table_layouts_finds_grid_cells():
    pytest.importorskip("cv2")
    layouts = detect_table_layouts(_table_fixture())

    assert len(layouts) == 1
    assert layouts[0].rows == 2
    assert layouts[0].cols == 2
    assert len(layouts[0].cells) == 4


def test_logical_table_layout_collapses_hierarchy_indent_lines():
    pytest.importorskip("cv2")
    image = _hierarchical_indent_table_fixture()
    layout = detect_table_layouts(image)[0]

    logical = logical_table_layout(image, layout)
    markdown = table_words_to_markdown(
        logical,
        [
            {"text": "Index", "bbox": (60, 34, 110, 50), "conf": 98},
            {"text": "Name", "bbox": (210, 34, 260, 50), "conf": 98},
            {"text": "Competencies", "bbox": (390, 34, 490, 50), "conf": 98},
            {"text": "B1.O.01", "bbox": (35, 113, 95, 130), "conf": 98},
            {"text": "Math", "bbox": (180, 113, 230, 130), "conf": 98},
            {"text": "UK-1", "bbox": (340, 113, 380, 130), "conf": 98},
        ],
    )

    assert logical.cols == 3
    assert "| Index | Name | Competencies |" in markdown
    assert "| B1.O.01 | Math | UK-1 |" in markdown


def test_table_layout_to_markdown_ocr_cells_in_reading_order():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    texts = iter(["Предмет", "Часы", "Math", "42"])

    markdown = table_layout_to_markdown(_table_fixture(), layout, lambda _cell: next(texts))

    assert markdown == "| Предмет | Часы |\n| --- | --- |\n| Math | 42 |"


def test_table_words_to_markdown_maps_ocr_words_to_cells():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    words = [
        {"text": "Предмет", "bbox": (55, 52, 120, 75), "conf": 98},
        {"text": "Часы", "bbox": (255, 52, 310, 75), "conf": 98},
        {"text": "Math", "bbox": (55, 142, 110, 165), "conf": 98},
        {"text": "42", "bbox": (255, 142, 285, 165), "conf": 98},
    ]

    markdown = table_words_to_markdown(layout, words)

    assert markdown == "| Предмет | Часы |\n| --- | --- |\n| Math | 42 |"


def test_table_words_to_markdown_normalizes_curriculum_index_column():
    pytest.importorskip("cv2")
    layout = detect_table_layouts(_table_fixture())[0]
    words = [
        {"text": "Индекс", "bbox": (55, 52, 120, 75), "conf": 98},
        {"text": "Наименование", "bbox": (255, 52, 350, 75), "conf": 98},
        {"text": "51.0.01", "bbox": (55, 142, 120, 165), "conf": 98},
        {"text": "Иностранный", "bbox": (255, 142, 340, 165), "conf": 98},
    ]

    markdown = table_words_to_markdown(layout, words)

    assert "| Б1.О.01 | Иностранный |" in markdown


def test_wide_curriculum_table_markdown_repairs_ocr_index_noise():
    x_lines = tuple(index * 24 for index in range(51))
    y_lines = tuple(index * 24 for index in range(8))
    layout = TableLayout(
        bbox=(0, 0, x_lines[-1], y_lines[-1]),
        rows=len(y_lines) - 1,
        cols=len(x_lines) - 1,
        x_lines=x_lines,
        y_lines=y_lines,
        cells=(),
    )
    words = [
        {"text": "Индекс", "bbox": (2, 50, 40, 64), "conf": 95},
        {"text": "Наименование", "bbox": (28, 50, 44, 64), "conf": 95},
        {"text": "Блок 1.Дисциплины", "bbox": (2, 74, 20, 88), "conf": 95},
        {"text": "(модули)", "bbox": (28, 74, 44, 88), "conf": 95},
        {"text": "Обязательная часть", "bbox": (2, 98, 20, 112), "conf": 95},
        {"text": "61.0.01", "bbox": (2, 122, 20, 136), "conf": 95},
        {"text": "Иностранный язык", "bbox": (28, 122, 44, 136), "conf": 95},
        {"text": "Б61.0.02", "bbox": (2, 146, 20, 160), "conf": 95},
        {"text": "История", "bbox": (28, 146, 44, 160), "conf": 95},
    ]

    markdown = wide_curriculum_table_to_markdown(layout, words)

    assert "| Б1 | Дисциплины (модули) |" in markdown
    assert "| Б1.О | Обязательная часть |" in markdown
    assert "| Б1.О.01 | Иностранный язык |" in markdown
    assert "| Б1.О.02 | История |" in markdown


def test_analyze_document_layout_returns_isolated_table_region():
    pytest.importorskip("cv2")
    regions = analyze_document_layout(_table_fixture())

    assert len(regions) == 1
    assert regions[0].kind == "table"
    assert regions[0].table is not None
    assert regions[0].table.bbox[0] == 0
    assert regions[0].table.rows == 2


def test_detect_table_layouts_handles_scan_like_noise():
    pytest.importorskip("cv2")
    layouts = detect_table_layouts(_noisy_table_fixture())

    assert len(layouts) == 1
    assert layouts[0].rows == 3
    assert layouts[0].cols == 3


def test_detect_table_layouts_ignores_blank_page():
    pytest.importorskip("cv2")
    assert detect_table_layouts(Image.new("RGB", (500, 300), "white")) == []


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
