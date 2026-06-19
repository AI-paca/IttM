import asyncio

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    LayoutRegion,
    TableCell,
    TableLayout,
    analyze_document_layout,
    detect_table_layouts,
    erase_table_lines_for_ocr,
    logical_table_layout,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_markdown,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)
from app.formatting.markdown_formatter import MarkdownFormatter
from app.layout.contracts import LayoutDecision, LayoutStageSpec
from app.pipeline_config import LayoutPipelineConfig, OcrPipelineProfile
from app.services import convert_service
from tests.support.generated_media import functional_ocr_fixture_image


@pytest.mark.parametrize("engine_type", ["browser", "tesserat"])
def test_engine_factory_rejects_unknown_selectors(engine_type):
    with pytest.raises(ValueError, match="Unknown OCR engine"):
        convert_service._create_engine(engine_type, OcrPipelineProfile(name="test"))


def test_iter_convert_bytes_reports_empty_pages(monkeypatch):
    image = Image.new("RGB", (100, 50), "white")

    class FakeEngine:
        def info(self):
            return {"engine": "fake"}

    monkeypatch.setattr(
        convert_service,
        "_create_engine",
        lambda _engine_type, _profile: FakeEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "_iter_document_images",
        lambda _content, _filename, _pipeline: iter([image]),
    )
    monkeypatch.setattr(
        convert_service,
        "_convert_page",
        lambda _image, _engine, _profile: (
            "",
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 0,
                "table_cells": 0,
            },
        ),
    )

    events = list(
        convert_service.iter_convert_bytes(
            b"image bytes",
            "test.png",
            pipeline_profile=OcrPipelineProfile(name="test"),
        )
    )

    assert events[0] == {
        "type": "warning",
        "code": "EMPTY_PAGE",
        "message": "No text was recognized on page 1.",
        "page": 1,
    }
    assert events[1] == {"type": "page", "page": 1, "markdown": ""}
    assert events[2]["type"] == "complete"
    assert events[2]["meta"]["empty_pages"] == [1]


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


def _generated_long_screenshot(width=620, card_count=48):
    card_height = 160
    gap_height = 28
    height = card_count * (card_height + gap_height)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    marker_colors = []

    for index in range(card_count):
        top = index * (card_height + gap_height)
        marker = (
            20 + index % 200,
            30 + (index * 3) % 190,
            40 + (index * 7) % 180,
        )
        marker_colors.append(marker)
        draw.rectangle((8, top + 8, 18, top + 18), fill=marker)
        draw.rectangle((30, top + 10, width - 30, top + 145), outline="black", width=2)
        draw.text((45, top + 35), f"PRODUCT-{index:03d}", fill="black")
        draw.text((45, top + 85), f"{1000 + index}.99", fill="black")

    return image, marker_colors


def _image_colors(image):
    return {color for _, color in image.getcolors(maxcolors=image.width * image.height)}


def test_long_screenshot_segmentation_preserves_every_generated_card_marker():
    image, marker_colors = _generated_long_screenshot()
    chunks = split_vertical(image, chunk_height=1200, overlap=100)

    try:
        chunk_colors = [_image_colors(chunk.convert("RGB")) for chunk in chunks]
        for marker in marker_colors:
            assert any(marker in colors for colors in chunk_colors), marker
        assert len(chunks) > 1
        assert max(chunk.height for chunk in chunks) <= 1600
    finally:
        for chunk in chunks:
            if chunk is not image:
                chunk.close()
        image.close()


def test_blank_band_chunking_coalesces_small_content_instead_of_dropping_it():
    image, marker_colors = _generated_long_screenshot(card_count=12)
    chunks = split_by_blank_bands(image, min_chunk_height=300)

    try:
        pixels = [_image_colors(chunk.convert("RGB")) for chunk in chunks]
        assert all(any(marker in colors for colors in pixels) for marker in marker_colors)
        assert all(chunk.height >= 300 for chunk in chunks[:-1])
    finally:
        for chunk in chunks:
            chunk.close()
        image.close()


def test_convert_page_segments_extreme_long_screenshot_before_layout(monkeypatch):
    image, _ = _generated_long_screenshot(card_count=64)
    layout_sizes = []
    recognized = []

    class FakeEngine:
        def recognize(self, chunk, mode="text_mode", psm=6):
            recognized.append(chunk.size)
            return f"segment-{len(recognized)}"

    def fake_layout(chunk, min_confirmed_cell_ratio=0.0):
        layout_sizes.append(chunk.size)
        return [LayoutRegion(kind="image", image=chunk, bbox=(0, 0, *chunk.size))]

    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)
    profile = OcrPipelineProfile(
        name="long-screenshot-test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
    )

    try:
        markdown, meta = convert_service._convert_page(image, FakeEngine(), profile)
    finally:
        image.close()

    assert len(layout_sizes) > 1
    assert max(height for _, height in layout_sizes) <= 1600
    assert len(recognized) == len(layout_sizes)
    assert "segment-1" in markdown
    assert meta["chunks"] == len(recognized)


def test_overlapping_starts_cover_final_edge_without_duplicates():
    assert convert_service._overlapping_starts(100, 40, 10) == [0, 30, 60]
    assert convert_service._overlapping_starts(101, 40, 10) == [0, 30, 60, 61]
    assert convert_service._overlapping_starts(20, 40, 10) == [0]


def test_wide_sparse_cover_uses_document_psm():
    image = Image.new("RGB", (3500, 2480), "white")
    ImageDraw.Draw(image).text((100, 100), "Учебный план", fill="black")
    profile = OcrPipelineProfile(
        name="test",
        document_region_psm=3,
        wide_text_region_psm=11,
    )

    try:
        assert convert_service._text_psm_for_image_region(image, profile) == 3
    finally:
        image.close()


def test_wide_dense_page_uses_sparse_layout_psm():
    image = Image.new("RGB", (3500, 2480), (200, 200, 200))
    profile = OcrPipelineProfile(
        name="test",
        document_region_psm=3,
        wide_text_region_psm=11,
    )

    try:
        assert convert_service._text_psm_for_image_region(image, profile) == 11
    finally:
        image.close()


def test_dense_grid_detection_accepts_large_landscape_table():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (2000, 1400), "white")
    draw = ImageDraw.Draw(image)
    for x in range(40, 1961, 120):
        draw.line((x, 40, x, 1360), fill="black", width=3)
    for y in range(40, 1361, 80):
        draw.line((40, y, 1960, y), fill="black", width=3)

    try:
        assert convert_service._looks_like_dense_grid_page(image) is True
    finally:
        image.close()


def test_dense_grid_detection_rejects_large_plain_image():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (2000, 1400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "ordinary landscape document", fill="black")

    try:
        assert convert_service._looks_like_dense_grid_page(image) is False
    finally:
        image.close()


def test_sparse_cover_detection_accepts_wide_low_ink_page():
    image = Image.new("RGB", (2400, 1600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "Sparse cover", fill="black")

    try:
        assert convert_service._looks_like_sparse_cover_page(image) is True
    finally:
        image.close()


def test_sparse_cover_detection_rejects_dense_page():
    image = Image.new("RGB", (2400, 1600), "black")
    try:
        assert convert_service._looks_like_sparse_cover_page(image) is False
    finally:
        image.close()


def test_convert_page_appends_dense_grid_fallback(monkeypatch):
    image = Image.new("RGB", (2400, 1600), "white")
    totals = {
        "chunks": 2,
        "cards_found": 0,
        "tables_found": 1,
        "table_cells": 20,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_dense_grid_page",
        lambda _image: True,
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_dense_grid_page",
        lambda fallback_engine, _image, _profile: (
            "fallback text" if fallback_engine is engine else "wrong engine",
            7,
        ),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 9
    assert meta["tables_found"] == 1


def test_convert_page_appends_sparse_cover_fallback(monkeypatch):
    image = Image.new("RGB", (2400, 1600), "white")
    totals = {
        "chunks": 1,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_dense_grid_page",
        lambda _image: False,
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_sparse_cover_page",
        lambda _image: True,
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_sparse_cover_page",
        lambda fallback_engine, _image, _profile: (
            "fallback text" if fallback_engine is engine else "wrong engine",
            4,
        ),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 5


def test_convert_page_appends_projector_slide_language_fallback(monkeypatch):
    image = Image.new("RGB", (2000, 1200), "white")
    totals = {
        "chunks": 1,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    monkeypatch.setattr(
        convert_service,
        "_convert_page_segment",
        lambda _image, _engine, _profile: ("primary text", totals.copy()),
    )
    monkeypatch.setattr(
        convert_service,
        "_recognize_projector_slide_fallback",
        lambda fallback_engine, _image, _profile: ("fallback text" if fallback_engine is engine else "wrong engine"),
    )
    engine = object()

    try:
        markdown, meta = convert_service._convert_page(
            image,
            engine,
            OcrPipelineProfile(name="test", dense_grid_fallback=True),
        )
    finally:
        image.close()

    assert "primary text" in markdown
    assert "fallback text" in markdown
    assert meta["chunks"] == 2


def test_convert_page_bounds_long_screenshot_before_spatial_layout(
    monkeypatch,
):
    image, _ = _generated_long_screenshot(card_count=64)
    layout_inputs = []
    region_inputs = []
    layout_crops = []

    class FakeEngine:
        def recognize(self, chunk, mode="text_mode", psm=6):
            raise AssertionError("test replaces region recognition")

    def fake_analyze_layout(
        page,
        config,
        *,
        min_confirmed_cell_ratio,
    ):
        layout_inputs.append(page.size)
        assert config.selector == "uniform_spatial_v1"
        assert min_confirmed_cell_ratio == 0.35
        first = page.crop((0, 0, page.width, page.height // 2))
        second = page.crop((0, page.height // 2, page.width, page.height))
        layout_crops.extend((first, second))
        return (
            [
                LayoutRegion(
                    kind="image",
                    image=first,
                    bbox=(0, 0, page.width, page.height // 2),
                ),
                LayoutRegion(
                    kind="image",
                    image=second,
                    bbox=(0, page.height // 2, page.width, page.height),
                ),
            ],
            LayoutDecision(
                label="spatial",
                stages=(LayoutStageSpec(name="spatial_regions"),),
                confidence=1.0,
            ),
        )

    monkeypatch.setattr(convert_service, "analyze_layout", fake_analyze_layout)

    def fake_recognize_region(_engine, region, _profile):
        region_inputs.append(region.size)
        return [f"region-{len(region_inputs)}"], 1, 0

    monkeypatch.setattr(
        convert_service,
        "_recognize_image_region",
        fake_recognize_region,
    )
    profile = OcrPipelineProfile(
        name="adaptive-long-screenshot",
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    )

    try:
        markdown, meta = convert_service._convert_page(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert len(layout_inputs) > 1
    assert max(height for _, height in layout_inputs) <= 1600
    assert len(region_inputs) == len(layout_inputs) * 2
    assert max(height for _, height in region_inputs) <= 800
    assert "region-1" in markdown
    assert "region-2" in markdown
    assert meta["chunks"] == len(region_inputs)
    for crop in layout_crops:
        with pytest.raises(ValueError):
            crop.getpixel((0, 0))


def test_convert_layout_region_honors_direct_ocr_decision_parameter():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "direct region text"

    image = Image.new("RGB", (600, 2400), "white")
    region = LayoutRegion(
        kind="image",
        image=image,
        bbox=(0, 0, 600, 2400),
    )
    try:
        parts, meta = convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(name="test"),
            (("direct_region_ocr", True),),
        )
    finally:
        image.close()

    assert parts == ["direct region text"]
    assert calls == [((600, 2400), "text_mode", 6)]
    assert meta["chunks"] == 1


def test_upscaled_mobile_screen_region_uses_sparse_page_psm():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "coupon text"

    image = Image.new("RGB", (1510, 2144), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            FakeEngine(),
            image,
            OcrPipelineProfile(name="test"),
        )
    finally:
        image.close()

    assert parts == ["coupon text"]
    assert chunks == 1
    assert cards == 0
    assert calls == [((1510, 2144), "text_mode", 3)]


def test_dewarped_projector_slide_region_uses_sparse_page_psm():
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "projector slide text"

    image = Image.new("RGB", (2000, 1200), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            FakeEngine(),
            image,
            OcrPipelineProfile(name="test"),
        )
    finally:
        image.close()

    assert parts == ["projector slide text"]
    assert chunks == 1
    assert cards == 0
    assert calls == [((2000, 1200), "text_mode", 3)]


def test_sparse_easyocr_region_uses_profile_tesseract_fallback(monkeypatch):
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", image.size, mode, psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", image.size, mode, psm))
            return (
                "Схема сбора статистической отчетности о работе судов "
                "Судебный департамент Федеральное хранилище судебной статистики "
                "Районные суды Мировые судьи"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )
    image = Image.new("RGB", (2000, 1200), "white")
    try:
        parts, chunks, cards = convert_service._recognize_image_region(
            PrimaryEngine(),
            image,
            OcrPipelineProfile(
                name="test",
                sparse_text_fallback_engine="tesseract",
                sparse_text_fallback_min_tokens=8,
                sparse_text_fallback_min_ratio=1.25,
            ),
        )
    finally:
        image.close()

    assert parts == [
        (
            "weak\n\n"
            "Схема сбора статистической отчетности о работе судов "
            "Судебный департамент Федеральное хранилище судебной статистики "
            "Районные суды Мировые судьи"
        )
    ]
    assert chunks == 1
    assert cards == 0
    assert calls == [
        ("primary", (2000, 1200), "text_mode", 3),
        ("fallback", (2000, 1200), "text_mode", 3),
    ]


def test_extra_pass_engine_uses_declared_recovery_engine(monkeypatch):
    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

    recovery = object()
    monkeypatch.setattr(
        "app.engines.tesseract_engine.TesseractEngine",
        lambda **kwargs: (recovery, kwargs),
    )
    profile = OcrPipelineProfile(
        name="easyocr-with-recovery",
        sparse_text_fallback_engine="tesseract",
    )

    selected, kwargs = convert_service._extra_pass_engine(
        PrimaryEngine(),
        profile,
        language_priority=("rus", "eng"),
        ocr_border_pixels=0,
    )

    assert selected is recovery
    assert kwargs["language_priority"] == ("rus", "eng")
    assert kwargs["ocr_border_pixels"] == 0
    assert convert_service._engine_chain(PrimaryEngine(), profile) == [
        "easyocr",
        "tesseract",
    ]


def test_edge_word_uses_single_token_profile_fallback(monkeypatch):
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", mode, psm))
            return ""

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", mode, psm))
            return "SAMPLE"

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "_looks_like_edge_to_edge_word",
        lambda image: True,
    )
    image = Image.new("RGB", (3840, 2160), "white")
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            PrimaryEngine(),
            OcrPipelineProfile(
                name="edge-word",
                sparse_text_fallback_engine="tesseract",
                sparse_text_fallback_min_tokens=18,
                edge_word_fallback_min_tokens=1,
            ),
        )
    finally:
        image.close()

    assert markdown == "SAMPLE"
    assert meta["chunks"] == 1
    assert calls == [
        ("primary", "text_mode", 3),
        ("fallback", "text_mode", 3),
    ]


def test_dewarped_projector_slide_bypasses_spatial_layout(monkeypatch):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append((image.size, mode, psm))
            return "full projector slide text"

    def fail_layout(*_args, **_kwargs):
        raise AssertionError("dewarped projector slides should not be split")

    monkeypatch.setattr(convert_service, "analyze_layout", fail_layout)
    profile = OcrPipelineProfile(
        name="projector-spatial",
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
        ),
    )
    image = Image.new("RGB", (2000, 1200), "white")
    try:
        markdown, meta = convert_service._convert_page_segment(
            image,
            FakeEngine(),
            profile,
        )
    finally:
        image.close()

    assert markdown == "full projector slide text"
    assert meta["chunks"] == 1
    assert calls == [((2000, 1200), "text_mode", 3)]


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


def _bar_chart_fixture() -> Image.Image:
    image = Image.new("RGB", (900, 700), (8, 32, 92))
    draw = ImageDraw.Draw(image)

    draw.text((30, 30), "Top devices", fill="white")
    for index in range(10):
        top = 100 + index * 52
        right = 790 - index * 24
        draw.rectangle((90, top, right, top + 36), fill=(20, 120 + index * 8, 240))
        draw.text((40, top + 8), str(index + 1), fill="white")
        draw.text((105, top + 8), f"Device {index + 1}", fill="white")
        draw.text((right + 8, top + 8), str(1_800_000 - index * 90_000), fill="white")

    return image


def _dense_table_layout(width: int, height: int, *, rows: int, cols: int) -> TableLayout:
    x_lines = tuple(round(index * (width - 1) / cols) for index in range(cols + 1))
    y_lines = tuple(round(index * (height - 1) / rows) for index in range(rows + 1))
    cells = tuple(
        TableCell(
            row=row,
            col=col,
            bbox=(x_lines[col], y_lines[row], x_lines[col + 1], y_lines[row + 1]),
        )
        for row in range(rows)
        for col in range(cols)
    )
    return TableLayout(
        bbox=(0, 0, width, height),
        rows=rows,
        cols=cols,
        x_lines=x_lines,
        y_lines=y_lines,
        cells=cells,
    )


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


def test_mixed_10x14_table_keeps_placeholder_cells_and_raw_fallback():
    image = Image.new("RGB", (1000, 1400), "white")
    layout = _dense_table_layout(1000, 1400, rows=14, cols=10)
    words = []
    for col in range(10):
        left = col * 100 + 10
        words.append(
            {
                "text": f"H{col + 1}",
                "bbox": (left, 20, left + 40, 50),
                "conf": 98,
            }
        )
    words.extend(
        [
            {"text": "й-A1-EN-001", "bbox": (110, 125, 190, 150), "conf": 98},
            {"text": "Привет", "bbox": (210, 125, 270, 150), "conf": 98},
            {"text": "Sample", "bbox": (310, 125, 370, 150), "conf": 98},
            {"text": "中文", "bbox": (410, 125, 450, 150), "conf": 98},
            {
                "text": "РАЗДЕЛ A SECTION ALPHA merged subsection й-ALPHA-2026",
                "bbox": (10, 225, 990, 255),
                "conf": 98,
            },
        ]
    )

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            assert psm == 6
            return words

        def recognize(self, image, mode="text_mode", psm=6):
            assert psm == 11
            return "raw mixed fallback 中文 Fake blocks 909"

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_min_word_cell_coverage=0.0,
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=14,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        FakeEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)
    table_lines = [line for line in markdown.splitlines() if line.startswith("|")]
    section_line = next(line for line in table_lines if "РАЗДЕЛ A SECTION ALPHA" in line)

    assert len(section_line.strip()[1:-1].split("|")) == 10
    assert "raw mixed fallback 中文 Fake blocks 909" in markdown
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 140


def test_wide_easyocr_table_raw_fallback_uses_sparse_tesseract(monkeypatch):
    image = Image.new("RGB", (2600, 1000), "white")
    layout = _dense_table_layout(2600, 1000, rows=10, cols=26)
    words = [
        {
            "text": f"Б1.О.{index:02d}",
            "bbox": (index * 90 + 8, 18, index * 90 + 74, 42),
            "conf": 95,
        }
        for index in range(26)
    ]
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize_words(self, image, psm=6, min_conf=20):
            return words

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", psm))
            return (
                "Математический анализ Линейная алгебра "
                "Дифференциальные уравнения Дискретная математика "
                "Теория вероятностей Методы оптимизации Теория управления"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_min_word_cell_coverage=0.0,
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=30,
        table_raw_text_fallback_psm=11,
        sparse_text_fallback_engine="tesseract",
        sparse_text_fallback_min_tokens=6,
        sparse_text_fallback_min_ratio=1.25,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        PrimaryEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)

    assert "Дифференциальные уравнения" in markdown
    assert calls == [("primary", 11), ("fallback", 11)]
    assert meta["chunks"] >= 2
    assert meta["tables_found"] == 1


def test_wide_easyocr_table_without_markdown_uses_table_raw_sparse_fallback(
    monkeypatch,
):
    image = Image.new("RGB", (2600, 1000), "white")
    layout = _dense_table_layout(2600, 1000, rows=10, cols=26)
    calls = []

    class PrimaryEngine:
        def info(self):
            return {"engine": "easyocr"}

        def recognize_words(self, image, psm=6, min_conf=20):
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("primary", psm))
            return "weak"

    class FallbackEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(("fallback", psm))
            return (
                "Математический анализ Линейная алгебра "
                "Дифференциальные уравнения Дискретная математика "
                "Теория вероятностей Методы оптимизации Теория управления"
            )

    monkeypatch.setattr(
        convert_service,
        "_create_sparse_text_fallback_engine",
        lambda profile: FallbackEngine(),
    )

    profile = OcrPipelineProfile(
        name="test",
        table_layout_normalization="preserve_grid",
        table_raw_text_fallback=True,
        table_raw_text_fallback_min_rows=10,
        table_raw_text_fallback_min_cols=8,
        table_raw_text_fallback_max_cols=30,
        table_raw_text_fallback_min_ratio=0.75,
        table_raw_text_fallback_psm=11,
        sparse_text_fallback_engine="tesseract",
        sparse_text_fallback_min_tokens=6,
    )
    parts, meta = convert_service._convert_layout_region(
        LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout),
        PrimaryEngine(),
        profile,
    )

    markdown = "\n\n".join(parts)

    assert "Дифференциальные уравнения" in markdown
    assert calls == [("primary", 11), ("fallback", 11)]
    assert meta["chunks"] >= 2
    assert meta["tables_found"] == 1


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


def test_detect_table_layouts_ignores_text_contours_inside_generated_grid():
    pytest.importorskip("cv2")
    image = functional_ocr_fixture_image("generated-product-table")
    try:
        layouts = detect_table_layouts(
            image,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        image.close()

    assert len(layouts) == 1
    assert layouts[0].rows == 3
    assert layouts[0].cols == 4


def test_erase_table_lines_preserves_generated_table_text_strokes():
    pytest.importorskip("cv2")
    image = functional_ocr_fixture_image("generated-product-table")
    cleaned = erase_table_lines_for_ocr(image)
    try:
        original_gray = np.asarray(image.convert("L"))
        cleaned_gray = np.asarray(cleaned.convert("L"))
        original_text_ink = original_gray[250:350, 80:350] < 180
        cleaned_text_ink = cleaned_gray[250:350, 80:350] < 180

        assert np.count_nonzero(original_text_ink) > 100
        assert np.count_nonzero(cleaned_text_ink) >= np.count_nonzero(original_text_ink) * 0.75
        assert np.mean(cleaned_gray[90:500, 58:64]) > 245
    finally:
        if cleaned is not image:
            cleaned.close()
        image.close()


def test_detect_table_layouts_ignores_blank_page():
    pytest.importorskip("cv2")
    assert detect_table_layouts(Image.new("RGB", (500, 300), "white")) == []


def test_detect_table_layouts_rejects_bar_chart_as_grid():
    pytest.importorskip("cv2")
    assert (
        detect_table_layouts(
            _bar_chart_fixture(),
            min_confirmed_cell_ratio=0.35,
        )
        == []
    )


def test_convert_service_uses_table_layout_before_vertical_chunks(monkeypatch, tmp_path):
    pytest.importorskip("cv2")
    values = iter(["Предмет", "Часы", "Math", "42"])

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            return next(values)

        def info(self):
            return {"engine": "fake"}

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())

    image_path = tmp_path / "table.png"
    _table_fixture().save(image_path)

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=OcrPipelineProfile(
                name="test",
                layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
            ),
        )
    )

    assert "| Предмет | Часы |" in markdown
    assert "| Math | 42 |" in markdown
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 4


def test_convert_service_segments_implausibly_tall_table_region(monkeypatch, tmp_path):
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            raise AssertionError("tall pseudo-table reached table OCR")

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return f"chunk {len(recognized_sizes)}"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        assert min_confirmed_cell_ratio == 0.42
        layout = _dense_table_layout(image.width, image.height, rows=21, cols=25)
        return [LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout)]

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "long-screenshot.png"
    Image.new("RGB", (800, 4200), (230, 230, 230)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        grid_min_confirmed_cell_ratio=0.42,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert "chunk 1" in markdown
    assert len(recognized_sizes) > 1
    assert max(height for _, height in recognized_sizes) <= 1200
    assert meta["chunks"] == len(recognized_sizes)
    assert meta["tables_found"] == 0
    assert meta["table_cells"] == 0


def test_convert_service_bounds_large_table_fallback(monkeypatch, tmp_path):
    word_calls = []
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            word_calls.append(image.size)
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return "fallback text"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        assert min_confirmed_cell_ratio == 0.0
        layout = _dense_table_layout(image.width, image.height, rows=21, cols=25)
        return [LayoutRegion(kind="table", image=image, bbox=layout.bbox, table=layout)]

    monkeypatch.setattr(convert_service, "AutoEngine", lambda **_kwargs: FakeEngine())
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "large-table.png"
    Image.new("RGB", (800, 1000), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert "fallback text" in markdown
    assert len(word_calls) == 1
    assert recognized_sizes == [(800, 1000)]
    assert meta["chunks"] == 2
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 525


def test_convert_service_rejects_sparse_table_markdown(monkeypatch, tmp_path):
    recognized_sizes = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [{"text": "lonely", "bbox": (20, 20, 80, 50)}]

        def recognize(self, image, mode="text_mode", psm=6):
            recognized_sizes.append(image.size)
            return "raw ranking with names and numbers"

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        layout = _dense_table_layout(image.width, image.height, rows=12, cols=12)
        return [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ]

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "sparse-pseudo-table.png"
    Image.new("RGB", (900, 700), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        table_min_word_cell_coverage=0.35,
        max_table_cell_ocr_calls=16,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw ranking with names and numbers"
    assert recognized_sizes == [(900, 700)]
    assert meta["chunks"] == 2
    assert meta["tables_found"] == 1
    assert meta["table_cells"] == 144


def test_convert_service_rejects_sparse_cell_markdown(monkeypatch, tmp_path):
    cell_calls = 0
    raw_calls = 0

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return []

        def recognize(self, image, mode="text_mode", psm=6):
            nonlocal cell_calls, raw_calls
            if image.size == (400, 400):
                raw_calls += 1
                return "raw names 1 2 3 4"
            cell_calls += 1
            return "only one cell" if cell_calls == 1 else ""

        def info(self):
            return {"engine": "fake"}

    def fake_layout(image, min_confirmed_cell_ratio=0.0):
        layout = _dense_table_layout(image.width, image.height, rows=4, cols=4)
        return [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ]

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(convert_service, "analyze_document_layout", fake_layout)

    image_path = tmp_path / "sparse-small-table.png"
    Image.new("RGB", (400, 400), (200, 200, 200)).save(image_path)
    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        max_table_cell_ocr_calls=16,
        table_min_cell_coverage=0.5,
    )

    markdown, meta = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw names 1 2 3 4"
    assert cell_calls == 16
    assert raw_calls == 1
    assert meta["chunks"] == 18


def test_convert_service_checks_wide_table_coverage_before_formatting(
    monkeypatch,
    tmp_path,
):
    formatted = False

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [{"text": "Б1.О.01", "bbox": (5, 5, 20, 20)}]

        def recognize(self, image, mode="text_mode", psm=6):
            return "raw curriculum text"

        def info(self):
            return {"engine": "fake"}

    image_path = tmp_path / "sparse-wide-table.png"
    Image.new("RGB", (1000, 200), (200, 200, 200)).save(image_path)
    layout = _dense_table_layout(1000, 200, rows=2, cols=50)

    monkeypatch.setattr(
        convert_service,
        "AutoEngine",
        lambda **_kwargs: FakeEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "analyze_document_layout",
        lambda image, min_confirmed_cell_ratio=0.0: [
            LayoutRegion(
                kind="table",
                image=image,
                bbox=layout.bbox,
                table=layout,
            )
        ],
    )

    def fake_wide_formatter(table, words):
        nonlocal formatted
        formatted = True
        return "| sparse |"

    original_formatter = convert_service.format_table_words

    def fake_formatter(name, table, words):
        if name == "curriculum":
            return fake_wide_formatter(table, words)
        return original_formatter(name, table, words)

    monkeypatch.setattr(
        convert_service,
        "format_table_words",
        fake_formatter,
    )

    profile = OcrPipelineProfile(
        name="test",
        layout=LayoutPipelineConfig(allowed_stages=("table_regions",)),
        wide_table_min_word_cell_coverage=0.02,
        max_table_cell_ocr_calls=16,
        table_layout_normalization="preserve_grid",
        table_word_recognition="single_pass_with_left_strip",
        table_word_formatters=("curriculum", "generic_markdown"),
    )
    markdown, _ = asyncio.run(
        convert_service.convert(
            image_path,
            engine_type="auto",
            pipeline_profile=profile,
        )
    )

    assert markdown == "raw curriculum text"
    assert formatted is False


def test_generic_profile_does_not_infer_curriculum_from_column_count(monkeypatch):
    layout = _dense_table_layout(1000, 200, rows=2, cols=50)
    calls = []

    class FakeEngine:
        def recognize_words(self, image, psm=6, min_conf=20):
            return [
                {
                    "text": "value",
                    "bbox": (5, 5, 20, 20),
                }
            ]

        def recognize(self, image, mode="text_mode", psm=6):
            return "raw table"

    region = LayoutRegion(
        kind="table",
        image=Image.new("RGB", (1000, 200), "white"),
        bbox=layout.bbox,
        table=layout,
    )
    original_formatter = convert_service.format_table_words

    def recording_formatter(name, table, words):
        calls.append(name)
        return original_formatter(name, table, words)

    monkeypatch.setattr(
        convert_service,
        "format_table_words",
        recording_formatter,
    )
    try:
        convert_service._convert_layout_region(
            region,
            FakeEngine(),
            OcrPipelineProfile(
                name="generic",
                table_min_word_cell_coverage=0,
            ),
        )
    finally:
        region.image.close()

    assert calls == ["generic_markdown"]
