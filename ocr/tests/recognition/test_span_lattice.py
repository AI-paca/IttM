from PIL import Image, ImageDraw

from app.chunking.vertical import TableCell, TableLayout
from app.recognition.span_lattice import (
    SpanFusionDecision,
    apply_repeated_span_identifier_crops,
    apply_slash_bounded_cjk_crops,
    fuse_horizontal_span_candidates,
    observed_slash_phrases,
    words_outside_fused_spans,
)


def _table() -> TableLayout:
    return TableLayout(
        bbox=(0, 0, 400, 120),
        rows=3,
        cols=4,
        x_lines=(0, 100, 200, 300, 400),
        y_lines=(0, 40, 80, 120),
        cells=tuple(
            TableCell(row=row, col=col, bbox=(col * 100, row * 40, (col + 1) * 100, (row + 1) * 40))
            for row in range(3)
            for col in range(4)
        ),
    )


def _span_image() -> Image.Image:
    image = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(image)
    for y in (0, 40, 80, 119):
        draw.line((0, y, 399, y), fill="black", width=2)
    for x in (0, 100, 200, 300, 399):
        draw.line((x, 0, x, 39), fill="black", width=2)
        draw.line((x, 80, x, 119), fill="black", width=2)
    return image


def _words(parts: tuple[str, ...], confidence: int = 90) -> list[dict]:
    return [
        {
            "text": text,
            "bbox": (10 + index * 95, 50, 90 + index * 95, 70),
            "conf": confidence,
        }
        for index, text in enumerate(parts)
    ]


def test_slash_phrases_retain_observed_text_bbox_and_confidence():
    phrases = observed_slash_phrases(
        [
            {"text": "SECTION/", "bbox": (10, 10, 100, 30), "conf": 80},
            {"text": "部分", "bbox": (110, 10, 150, 30), "conf": 90},
        ]
    )

    assert [phrase.text for phrase in phrases] == ["SECTION", "部分"]
    assert phrases[0].bbox[0] == 10
    assert phrases[0].confidence_milli == 800


def test_fuses_only_geometry_proven_span_from_observed_phrases():
    image = _span_image()
    table = _table()
    primary = _words(("РАЗДЕЛ /", "SECTION /", "#84} /", "tail"))
    candidates = (
        ("rus", _words(("РАЗДЕЛ /", "ЗЕСТОМ /", "#84} /", "хвост"), 75)),
        ("eng", _words(("PA3AE /", "SECTION /", "noise /", "tail"), 92)),
        ("chi_sim", _words(("noise /", "noise /", "部分乙 /", "noise"), 95)),
    )
    try:
        fused, decisions = fuse_horizontal_span_candidates(
            image,
            table,
            primary,
            candidates,
        )
    finally:
        image.close()

    assert len(decisions) == 1
    assert decisions[0].row == 1
    assert decisions[0].selected_text == "РАЗДЕЛ / SECTION / 部分乙 / tail"
    assert decisions[0].selected_sources == ("rus", "eng", "chi_sim", "eng")
    assert fused == [
        {
            "text": decisions[0].selected_text,
            "bbox": (0, 40, 400, 80),
            "conf": 100,
            "span_sources": decisions[0].selected_sources,
        }
    ]


def test_micro_cell_words_are_filtered_only_inside_fused_row():
    table = _table()
    decision = SpanFusionDecision(1, 0, 3, ("rus",), "observed")
    words = [
        {"text": "above", "bbox": (10, 10, 40, 30)},
        {"text": "span duplicate", "bbox": (10, 50, 80, 70)},
        {"text": "below", "bbox": (10, 90, 40, 110)},
        {"text": "boundary", "bbox": (10, 70, 40, 90)},
    ]

    assert [word["text"] for word in words_outside_fused_spans(words, table, (decision,))] == [
        "above",
        "below",
        "boundary",
    ]


class _CropEngine:
    def recognize_cjk_phrase(self, _image):
        return "部分甲", 91.0

    def recognize_identifier_segment(self, _image):
        return "ALPHA", 95.0


def _one_row_span_table() -> TableLayout:
    return TableLayout(
        bbox=(0, 0, 500, 60),
        rows=1,
        cols=5,
        x_lines=(0, 100, 200, 300, 400, 500),
        y_lines=(0, 60),
        cells=tuple(
            TableCell(row=0, col=column, bbox=(column * 100, 0, (column + 1) * 100, 60)) for column in range(5)
        ),
    )


def test_slash_bounded_cjk_crop_replaces_only_third_phrase():
    table = _one_row_span_table()
    raw = [
        {"text": "/", "bbox": (90, 15, 95, 40), "conf": 90},
        {"text": "/", "bbox": (190, 15, 195, 40), "conf": 90},
        {"text": "noise", "bbox": (210, 15, 280, 40), "conf": 50},
        {"text": "/", "bbox": (290, 15, 295, 40), "conf": 90},
        {"text": "/", "bbox": (390, 15, 395, 40), "conf": 90},
    ]
    text = "РАЗДЕЛ / SECTION ALPHA / noise / merged / й-AEPHA-2026"
    fused = [{"text": text, "bbox": (0, 0, 500, 60), "conf": 100}]
    span = SpanFusionDecision(0, 0, 4, ("rus", "eng", "eng", "eng", "rus"), text)
    image = Image.new("RGB", (500, 60), "white")
    try:
        result, decisions, calls = apply_slash_bounded_cjk_crops(_CropEngine(), image, table, raw, fused, (span,))
    finally:
        image.close()

    assert calls == 1
    assert decisions[0].selected == "部分甲"
    assert "SECTION ALPHA / 部分甲 / merged" in result[0]["text"]


def test_repeated_span_identifier_requires_label_and_crop_match():
    table = _one_row_span_table()
    raw = [{"text": "й-АЕРНА-2026", "bbox": (350, 15, 480, 40), "conf": 70}]
    text = "РАЗДЕЛ / SECTION ALPHA / 部分甲 / merged / й-AEPHA-2026"
    fused = [{"text": text, "bbox": (0, 0, 500, 60), "conf": 100}]
    span = SpanFusionDecision(0, 0, 4, ("rus", "eng", "chi_sim", "eng", "rus"), text)
    image = Image.new("RGB", (500, 60), "white")
    try:
        result, decisions, calls = apply_repeated_span_identifier_crops(
            _CropEngine(), image, table, raw, fused, (span,)
        )
    finally:
        image.close()

    assert calls == 1
    assert decisions[0].selected == "й-ALPHA-2026"
    assert result[0]["text"].endswith("й-ALPHA-2026")
