from types import SimpleNamespace

from PIL import Image

from app.pipeline_core import separated_recognition
from app.pipeline_core.separated import SeparatedOcrJob
from app.pipeline_core.separated_recognition import recognize_separated_block
from app.sparse_pipeline.ocr_adapter_contracts import OcrLanguageUnavailableError


class _OrderedWordEngine:
    def recognize_words_for_language(self, _crop, language, *, psm, min_conf):
        assert language == "rus+eng"
        assert psm == 6
        assert min_conf == 0
        return [
            {"text": "Then", "bbox": (0, 0, 30, 12), "conf": 99.0},
            {"text": "a", "bbox": (34, 4, 40, 12), "conf": 98.0},
            {"text": "medical", "bbox": (44, 0, 92, 12), "conf": 97.0},
            {"text": "answer", "bbox": (0, 25, 45, 36), "conf": 96.0},
            {"text": "2h", "bbox": (50, 24, 65, 36), "conf": 95.0},
        ]


def test_tesseract_tsv_order_survives_different_word_tops():
    job = SeparatedOcrJob(
        index=0,
        bbox=(0, 0, 100, 40),
        object_id=0,
        row=0,
        column=0,
        row_span=2,
        column_span=1,
        recognition_mode=0,
        object_kind=0,
        languages="rus+eng",
        transform="raw",
        depth=0,
        logical_row_count=2,
        logical_column_count=1,
        grammar_milli=0,
        superseded=False,
    )
    profile = SimpleNamespace(
        text_region_psm=6,
        document_region_psm=3,
        wide_text_region_psm=11,
    )
    crop = Image.new("RGB", (100, 40), "white")
    try:
        result = recognize_separated_block(
            crop,
            job,
            _OrderedWordEngine(),
            profile,
            lambda *_args, **_kwargs: "",
        )
    finally:
        crop.close()

    assert result.text == "Then a medical\nanswer 2h"
    assert [word.text for word in result.words] == [
        "Then",
        "a",
        "medical",
        "answer",
        "2h",
    ]


def test_missing_tesseract_language_becomes_an_empty_splay_candidate(monkeypatch):
    class _UnavailableWorker:
        def __init__(self, _config):
            raise OcrLanguageUnavailableError("Tesseract languages are missing: ell")

    monkeypatch.setattr(separated_recognition, "TesseractWorker", _UnavailableWorker)
    job = SeparatedOcrJob(
        index=0,
        bbox=(0, 0, 100, 40),
        object_id=0,
        row=0,
        column=0,
        row_span=1,
        column_span=1,
        recognition_mode=0,
        object_kind=0,
        languages="ell",
        transform="raw",
        depth=0,
        logical_row_count=1,
        logical_column_count=1,
        grammar_milli=0,
        superseded=False,
    )
    profile = SimpleNamespace(
        text_region_psm=6,
        document_region_psm=3,
        wide_text_region_psm=11,
    )
    crop = Image.new("RGB", (100, 40), "white")
    try:
        result = recognize_separated_block(
            crop,
            job,
            SimpleNamespace(recognize_with_psm=lambda *_args, **_kwargs: ""),
            profile,
            lambda *_args, **_kwargs: "",
        )
    finally:
        crop.close()

    assert result.text == ""
    assert result.words == ()
