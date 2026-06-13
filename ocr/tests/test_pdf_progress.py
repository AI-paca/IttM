import sys
from types import SimpleNamespace

from PIL import Image

from app.services.convert_service import _iter_document_images


class _IdentityPipeline:
    def apply(self, image):
        return image


def test_pdf_renderer_logs_each_page(monkeypatch, capsys):
    rendered_pages = []

    def convert_from_path(
        _path,
        *,
        dpi,
        fmt,
        first_page,
        last_page,
        thread_count,
    ):
        assert dpi == 300
        assert fmt == "png"
        assert first_page == last_page
        assert thread_count == 1
        rendered_pages.append(first_page)
        return [Image.new("RGB", (20, 10), "white")]

    monkeypatch.setitem(
        sys.modules,
        "pdf2image",
        SimpleNamespace(
            convert_from_path=convert_from_path,
            pdfinfo_from_path=lambda _path, first_page=None, last_page=None: (
                {"Pages": 2} if first_page is None else {f"Page {first_page} size": "612 x 792 pts"}
            ),
        ),
    )

    images = list(
        _iter_document_images(
            b"%PDF-1.4 test",
            "test.pdf",
            _IdentityPipeline(),
        )
    )
    try:
        assert rendered_pages == [1, 2]
        output = capsys.readouterr().out
        assert "[PDF] Rendering page 1/2" in output
        assert "[PDF] Rendering page 2/2" in output
    finally:
        for image in images:
            image.close()
