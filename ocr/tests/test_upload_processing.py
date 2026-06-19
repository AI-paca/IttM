import tempfile
from pathlib import Path

import pdf2image
import pytest
from PIL import Image

from app import upload_limits
from app.services import convert_service


def _image_bytes(image: Image.Image) -> bytes:
    from io import BytesIO

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_pdf_pages_are_rendered_one_at_a_time_and_spool_is_removed(
    monkeypatch, tmp_path
):
    calls = []

    class ImageOnlyPipeline:
        def apply(self, image):
            raise AssertionError(
                "image-only preprocessing must not alter rendered PDF pages"
            )

    def fake_pdfinfo(path, first_page=None, last_page=None):
        assert Path(path).exists()
        if first_page is not None:
            assert first_page == last_page
            return {f"Page {first_page} size": "595 x 842 pts"}
        return {"Pages": 3}

    def fake_convert(path, **kwargs):
        assert Path(path).exists()
        assert kwargs["dpi"] == 300
        assert "size" not in kwargs
        calls.append(
            (kwargs["first_page"], kwargs["last_page"], kwargs["thread_count"])
        )
        return [Image.new("RGB", (40, 30), "white")]

    monkeypatch.setattr(pdf2image, "pdfinfo_from_path", fake_pdfinfo)
    monkeypatch.setattr(pdf2image, "convert_from_path", fake_convert)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    pipeline = ImageOnlyPipeline()
    pages = list(
        convert_service._iter_document_images(b"%PDF-fake", "test.pdf", pipeline)
    )
    try:
        assert calls == [
            (1, 1, 1),
            (2, 2, 1),
            (3, 3, 1),
        ]
        assert len(pages) == 3
        assert list(tmp_path.iterdir()) == []
    finally:
        for page in pages:
            page.close()


def test_upload_limit_is_bounded_by_default_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("OCR_MAX_UPLOAD_BYTES", raising=False)
    assert upload_limits.max_upload_bytes() == 128 * 1024 * 1024

    monkeypatch.setenv("OCR_MAX_UPLOAD_BYTES", "1024")
    assert upload_limits.max_upload_bytes() == 1024

    monkeypatch.setenv("OCR_MAX_UPLOAD_BYTES", "0")
    assert upload_limits.max_upload_bytes() == 0


def test_decoded_image_limit_rejects_before_loading_pixels(monkeypatch):
    class HugeImage:
        size = (10_000, 10_000)

    monkeypatch.setenv("OCR_MAX_DECODED_IMAGE_PIXELS", "80000000")

    with pytest.raises(ValueError, match="100000000 pixels"):
        convert_service._validate_decoded_image_size(HugeImage())


def test_pdf_render_dpi_only_downscales_oversized_pages(monkeypatch):
    monkeypatch.setenv("OCR_MAX_PDF_RENDER_DIMENSION", "6000")

    assert convert_service._pdf_render_options({"Page size": "864 x 432 pts"}) == {
        "dpi": 300
    }
    assert convert_service._pdf_render_options({"Page size": "14400 x 7200 pts"}) == {
        "dpi": 30
    }


def test_pdf_text_layer_bypasses_ocr_when_usable(monkeypatch):
    monkeypatch.setattr(
        convert_service,
        "_extract_pdf_text_layer_pages",
        lambda _content, _filename: ["Учебный план\nБ1.О.01 История России"],
    )
    monkeypatch.setattr(
        convert_service,
        "_create_engine",
        lambda _engine_type: pytest.fail(
            "OCR engine should not be created for usable PDF text layer"
        ),
    )

    markdown, meta = convert_service.convert_bytes(
        b"%PDF-text-layer",
        filename="plan.pdf",
        engine_type="tesseract",
    )

    assert markdown == "Учебный план\nБ1.О.01 История России"
    assert meta["engine"] == "pdf_text_layer"
    assert meta["engine_chain"] == ["pdf_text_layer"]
    assert meta["pages"] == 1
    assert meta["chunks"] == 0
    assert meta["pdf_mode"] == "auto"


def test_pdf_raster_mode_skips_usable_text_layer(monkeypatch):
    monkeypatch.setattr(
        convert_service,
        "_extract_pdf_text_layer_pages",
        lambda _content, _filename: pytest.fail("raster mode must not inspect the PDF text layer"),
    )

    class FakeEngine:
        def info(self):
            return {"engine": "tesseract"}

    monkeypatch.setattr(
        convert_service,
        "_create_engine",
        lambda _engine_type, _profile: FakeEngine(),
    )
    monkeypatch.setattr(
        convert_service,
        "_iter_document_images",
        lambda _content, _filename, _pipeline: iter([Image.new("RGB", (40, 30), "white")]),
    )
    monkeypatch.setattr(
        convert_service,
        "_convert_page",
        lambda _image, _engine, _profile: (
            "OCR page",
            {
                "chunks": 1,
                "cards_found": 0,
                "tables_found": 0,
                "table_cells": 0,
            },
        ),
    )

    markdown, meta = convert_service.convert_bytes(
        b"%PDF-text-layer",
        filename="plan.pdf",
        engine_type="tesseract",
        pdf_mode="raster",
    )

    assert markdown == "OCR page"
    assert meta["engine"] == "tesseract"
    assert meta["engine_chain"] == ["tesseract"]
    assert meta["pdf_mode"] == "raster"


def test_pdf_mode_rejects_unknown_values():
    with pytest.raises(ValueError, match="Known modes: auto, raster"):
        convert_service.normalize_pdf_mode("slow-magic")


def test_pdf_text_layer_requires_enough_usable_pages():
    good_page = " ".join(f"слово{i}" for i in range(40))

    assert convert_service._usable_pdf_text_pages(["", "D (D D", good_page, ""]) == []
    assert convert_service._usable_pdf_text_pages([good_page, good_page]) == [
        good_page,
        good_page,
    ]
