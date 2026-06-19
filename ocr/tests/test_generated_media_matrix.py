import io
import os
import tempfile
from pathlib import Path

import pdf2image
import pytest
from PIL import Image, ImageDraw

from app.pipeline_config import OcrPipelineProfile
from app.services import convert_service
from tests.generated_media import (
    animated_gif_bytes,
    deterministic_mutations,
    exif_rotated_jpeg_bytes,
    image_bytes,
    multipage_tiff_bytes,
    text_image_bytes,
    transparent_text_png_bytes,
)

RAW_PROFILE = OcrPipelineProfile(name="generated-media")


def _load_pages(payload, filename):
    pipeline = convert_service.OcrPreprocessingPipeline.from_step_names(())
    return list(convert_service._iter_document_images(payload, filename, pipeline))


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (image_bytes(mode="1"), "mono.png"),
        (image_bytes(mode="I;16"), "wide-depth.png"),
        (
            text_image_bytes(
                image_format="JPEG",
                quality=1,
                progressive=True,
            ),
            "low-quality-progressive.jpg",
        ),
        (image_bytes(mode="CMYK", image_format="JPEG"), "cmyk.jpg"),
        (image_bytes(mode="L"), "grayscale.png"),
        (image_bytes(size=(1, 1)), "one-pixel.png"),
        (image_bytes(size=(1, 50_000)), "one-pixel-wide.png"),
    ],
)
def test_generated_raster_variants_decode_to_rgb_without_crashing(payload, filename):
    pages = _load_pages(payload, filename)
    try:
        assert len(pages) == 1
        assert pages[0].mode == "RGB"
        assert pages[0].width >= 1
        assert pages[0].height >= 1
    finally:
        for page in pages:
            page.close()


def test_exif_orientation_is_applied_before_ocr():
    pages = _load_pages(exif_rotated_jpeg_bytes(), "rotated.jpg")
    try:
        assert pages[0].size == (80, 160)
    finally:
        pages[0].close()


def test_transparent_image_is_flattened_without_losing_opaque_text():
    pages = _load_pages(transparent_text_png_bytes(), "alpha.png")
    try:
        assert pages[0].mode == "RGB"
        assert pages[0].getextrema()[0][0] < 255
    finally:
        pages[0].close()


@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (animated_gif_bytes(), "animated.gif"),
        (multipage_tiff_bytes(), "multipage.tiff"),
    ],
)
def test_multiframe_images_process_only_the_first_frame(payload, filename):
    pages = _load_pages(payload, filename)
    try:
        assert len(pages) == 1
    finally:
        pages[0].close()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00" * 512,
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        b"<svg><script>alert(1)</script></svg>",
        b"ftypheic" + b"\x00" * 64,
    ],
)
def test_invalid_or_unsupported_media_fails_as_value_error(payload):
    with pytest.raises(ValueError, match="Could not load image"):
        _load_pages(payload, "spoofed.png")


def test_deterministic_binary_mutations_never_escape_as_native_crashes():
    seed = text_image_bytes()
    outcomes = []
    mutation_count = 512 if os.environ.get("RUN_GENERATED_FUZZ") == "1" else 32

    for payload in deterministic_mutations(seed, count=mutation_count):
        try:
            pages = _load_pages(payload, "fuzz.png")
        except ValueError:
            outcomes.append("rejected")
        else:
            outcomes.append("decoded")
            for page in pages:
                page.close()

    assert len(outcomes) == mutation_count
    assert set(outcomes) <= {"decoded", "rejected"}


@pytest.mark.parametrize(
    ("size", "limit", "accepted"),
    [
        ((1, 1), 80_000_000, True),
        ((7680, 4320), 80_000_000, True),
        ((50_000, 1), 80_000_000, True),
        ((50_000, 50_000), 80_000_000, False),
    ],
)
def test_decoded_dimension_matrix_is_checked_without_allocating_pixels(
    monkeypatch,
    size,
    limit,
    accepted,
):
    class HeaderOnlyImage:
        pass

    image = HeaderOnlyImage()
    image.size = size
    monkeypatch.setenv("OCR_MAX_DECODED_IMAGE_PIXELS", str(limit))

    if accepted:
        convert_service._validate_decoded_image_size(image)
    else:
        with pytest.raises(ValueError, match="limit"):
            convert_service._validate_decoded_image_size(image)


def test_pdf_page_limit_fails_before_rendering_any_page(monkeypatch, tmp_path):
    render_calls = []

    monkeypatch.setenv("OCR_MAX_PDF_PAGES", "100")
    monkeypatch.setattr(
        pdf2image,
        "pdfinfo_from_path",
        lambda _path, first_page=None, last_page=None: {"Pages": 1000},
    )
    monkeypatch.setattr(
        pdf2image,
        "convert_from_path",
        lambda *_args, **_kwargs: render_calls.append(True),
    )
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(ValueError, match="1000 pages; limit is 100"):
        _load_pages(b"%PDF-generated", "many-pages.pdf")

    assert render_calls == []
    assert list(tmp_path.iterdir()) == []


def test_pdf_spool_ignores_traversal_filename_and_cleans_up_on_failure(
    monkeypatch,
    tmp_path,
):
    observed_paths = []

    def fail_pdfinfo(path, first_page=None, last_page=None):
        observed_paths.append(Path(path))
        raise RuntimeError("corrupt xref")

    monkeypatch.setattr(pdf2image, "pdfinfo_from_path", fail_pdfinfo)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(ValueError, match="Failed to process PDF"):
        _load_pages(b"%PDF-corrupt", "../../../etc/passwd.pdf")

    assert observed_paths[0].name == "document.pdf"
    assert observed_paths[0].parent.parent == tmp_path
    assert list(tmp_path.iterdir()) == []


def test_generated_long_receipt_reaches_engine_as_bounded_segments(monkeypatch):
    calls = []

    class FakeEngine:
        def recognize(self, image, mode="text_mode", psm=6):
            calls.append(image.size)
            return f"segment-{len(calls)}"

        def info(self):
            return {"engine": "fake"}

    image = Image.new("RGB", (500, 12_000), "white")
    draw = ImageDraw.Draw(image)
    for index in range(60):
        top = index * 190
        draw.text(
            (20, top + 20),
            f"ITEM-{index:03d} {index + 1}.99",
            fill="black",
        )

    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    monkeypatch.setattr(
        convert_service,
        "_create_engine",
        lambda _engine_type, _profile: FakeEngine(),
    )

    events = list(
        convert_service.iter_convert_bytes(
            output.getvalue(),
            "receipt.png",
            pipeline_profile=RAW_PROFILE,
        )
    )

    assert len(calls) > 1
    assert max(height for _, height in calls) <= 1600
    assert events[-1]["type"] == "complete"
    assert events[-1]["meta"]["chunks"] == len(calls)
