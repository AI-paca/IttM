import io
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import convert_service
from tests.quality_metrics import digit_sequence_recall, ordered_phrase_recall
from tests.visual_mutations import (
    close_images,
    quality_card_image,
    visual_mutations,
)

client = TestClient(app)


def test_visual_mutation_matrix_decodes_without_dimension_growth():
    source = quality_card_image()
    mutations = visual_mutations(source)
    pipeline = convert_service.OcrPreprocessingPipeline.from_step_names(())
    try:
        assert set(mutations) == {
            "low-contrast",
            "dark-mode",
            "salt-pepper",
            "watermark",
            "jpeg-ringing",
            "subpixel",
            "motion-blur",
            "glare",
            "perspective",
            "cropped-edge",
        }
        for name, image in mutations.items():
            payload = io.BytesIO()
            image.save(payload, format="PNG")
            pages = list(
                convert_service._iter_document_images(
                    payload.getvalue(),
                    f"{name}.png",
                    pipeline,
                )
            )
            try:
                assert len(pages) == 1
                assert pages[0].mode == "RGB"
                assert pages[0].width <= source.width
                assert pages[0].height <= source.height
            finally:
                pages[0].close()
    finally:
        close_images(mutations)
        source.close()


@pytest.mark.skipif(
    os.environ.get("RUN_OCR_QUALITY") != "1",
    reason="Visual OCR quality is part of the dedicated quality CI tier.",
)
@pytest.mark.parametrize(
    "variant",
    [
        "low-contrast",
        "dark-mode",
        "salt-pepper",
        "watermark",
        "jpeg-ringing",
        "subpixel",
        "motion-blur",
        "glare",
        "perspective",
        "cropped-edge",
    ],
)
def test_tesseract_preserves_identifiers_across_visual_mutations(variant):
    source = quality_card_image()
    mutations = visual_mutations(source)
    image = mutations[variant]
    try:
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        profile = "backend_tesseract_standard" if variant == "perspective" else "backend_raw"
        response = client.post(
            f"/convert?engine_type=tesseract&pipeline_profile={profile}",
            files={"file": (f"{variant}.png", payload.getvalue(), "image/png")},
        )
    finally:
        close_images(mutations)
        source.close()

    assert response.status_code == 200, response.text
    markdown = response.json()["markdown"]
    assert (
        ordered_phrase_recall(
            ["PRODUCT", "ALPHA", "TOTAL", "ORDER", "ZX-2026-42"],
            markdown,
        )
        >= 0.6
    )
    assert digit_sequence_recall("12345.67 2026 42", markdown) >= 0.6
