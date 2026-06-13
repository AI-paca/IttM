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


def test_pdf_pages_are_rendered_one_at_a_time_and_spool_is_removed(monkeypatch, tmp_path):
    calls = []

    class ImageOnlyPipeline:
        def apply(self, image):
            raise AssertionError("image-only preprocessing must not alter rendered PDF pages")

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
        calls.append((kwargs["first_page"], kwargs["last_page"], kwargs["thread_count"]))
        return [Image.new("RGB", (40, 30), "white")]

    monkeypatch.setattr(pdf2image, "pdfinfo_from_path", fake_pdfinfo)
    monkeypatch.setattr(pdf2image, "convert_from_path", fake_convert)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    pipeline = ImageOnlyPipeline()
    pages = list(convert_service._iter_document_images(b"%PDF-fake", "test.pdf", pipeline))
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


def test_upload_limit_is_opt_in(monkeypatch):
    monkeypatch.delenv("OCR_MAX_UPLOAD_BYTES", raising=False)
    assert upload_limits.max_upload_bytes() == 0

    monkeypatch.setenv("OCR_MAX_UPLOAD_BYTES", "1024")
    assert upload_limits.max_upload_bytes() == 1024


def test_decoded_image_limit_rejects_before_loading_pixels(monkeypatch):
    class HugeImage:
        size = (10_000, 10_000)

    monkeypatch.setenv("OCR_MAX_DECODED_IMAGE_PIXELS", "80000000")

    with pytest.raises(ValueError, match="100000000 pixels"):
        convert_service._validate_decoded_image_size(HugeImage())


def test_pdf_render_dpi_only_downscales_oversized_pages(monkeypatch):
    monkeypatch.setenv("OCR_MAX_PDF_RENDER_DIMENSION", "6000")

    assert convert_service._pdf_render_options({"Page size": "864 x 432 pts"}) == {"dpi": 300}
    assert convert_service._pdf_render_options({"Page size": "14400 x 7200 pts"}) == {"dpi": 30}
