from PIL import Image

from app.chunking.vertical import TableCell, TableLayout
from app.recognition.segment_crop_lattice import fuse_identifier_segment_crops


class _SegmentEngine:
    def __init__(self, results):
        self.results = iter(results)

    def recognize_identifier_segment(self, _image):
        return next(self.results)


def _table() -> TableLayout:
    return TableLayout(
        bbox=(0, 0, 240, 60),
        rows=1,
        cols=1,
        x_lines=(0, 240),
        y_lines=(0, 60),
        cells=(TableCell(row=0, col=0, bbox=(0, 0, 240, 60)),),
    )


def _word(text: str, confidence: float = 70) -> dict:
    return {
        "text": text,
        "bbox": (20, 15, 220, 45),
        "conf": confidence,
    }


def test_segment_crop_requires_whole_cell_corroboration_and_high_crop_confidence():
    image = Image.new("RGB", (240, 60), "white")
    engine = _SegmentEngine(
        (
            ("B2", 95.0),
            ("RU", 96.0),
            ("2026", 95.0),
        )
    )
    try:
        fused, decisions, calls = fuse_identifier_segment_crops(
            engine,
            image,
            _table(),
            [_word("й-B2-KY-2026")],
            (
                ("eng", [_word("u-B2-RU-2026", 0)]),
                ("chi_sim", [_word("-B2-RU-2026", 40)]),
            ),
        )
    finally:
        image.close()

    assert calls == 3
    assert [word["text"] for word in fused] == ["й-B2-RU-2026"]
    assert [(decision.segment, decision.primary, decision.selected) for decision in decisions] == [(2, "KY", "RU")]
    assert decisions[0].supporting_sources == ("chi_sim", "eng")


def test_low_confidence_crop_cannot_override_corroborated_text():
    image = Image.new("RGB", (240, 60), "white")
    engine = _SegmentEngine(
        (
            ("B2", 95.0),
            ("RU", 79.0),
            ("2026", 95.0),
        )
    )
    primary = [_word("й-B2-KY-2026")]
    try:
        fused, decisions, calls = fuse_identifier_segment_crops(
            engine,
            image,
            _table(),
            primary,
            (
                ("eng", [_word("u-B2-RU-2026", 0)]),
                ("chi_sim", [_word("-B2-RU-2026", 40)]),
            ),
        )
    finally:
        image.close()

    assert calls == 3
    assert fused == primary
    assert decisions == ()


def test_single_whole_cell_source_does_not_trigger_segment_reocr():
    image = Image.new("RGB", (240, 60), "white")
    engine = _SegmentEngine(())
    primary = [_word("й-B2-KY-2026")]
    try:
        fused, decisions, calls = fuse_identifier_segment_crops(
            engine,
            image,
            _table(),
            primary,
            (("chi_sim", [_word("-B2-RU-2026", 40)]),),
        )
    finally:
        image.close()

    assert calls == 0
    assert fused == primary
    assert decisions == ()
