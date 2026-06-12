import tempfile
from pathlib import Path

import pdf2image
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

    def fake_pdfinfo(path):
        assert Path(path).exists()
        return {"Pages": 3}

    def fake_convert(path, **kwargs):
        assert Path(path).exists()
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
