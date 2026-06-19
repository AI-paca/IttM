import os
from dataclasses import dataclass

import pytest

from app.engines.tesseract_engine import TesseractEngine
from app.pipeline_config import resolve_pipeline_profile
from app.services import convert_service
from tests.document_templates import (
    generate_document_templates,
    generate_long_cart,
)
from tests.generated_media import (
    FUNCTIONAL_OCR_FIXTURE_REGISTRY,
    functional_ocr_fixture_bytes,
    functional_ocr_fixture_spec,
)
from tests.quality_metrics import (
    digit_sequence_recall,
    markdown_table_shape,
    missing_tokens,
    name_value_pair_recall,
    ordered_phrase_recall,
    token_recall,
)
from tests.quality_fixtures import QUALITY_TEXT, write_quality_fixtures

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OCR_QUALITY") != "1",
    reason="OCR quality tests are heavy; set RUN_OCR_QUALITY=1 in debug.sh or GitHub Actions OCR job.",
)

EXPECTED_TOKENS = [
    "ABCXYZ",
    "abcxyz",
    "0123456789",
    "РУССКИЙ",
    "АБВГДЕЖЗ",
    "абвгдежз",
    "中文测试",
    "汉字识别",
    "MIXEDLATINД12345中文",
    "12345",
]


@dataclass(frozen=True)
class FunctionalQualityCase:
    fixture_id: str
    engine: str
    profile: str
    min_token_recall: float
    min_pair_recall: float = 1.0
    min_digit_recall: float = 1.0
    expected_pages: int = 1
    min_table_rows: int = 0
    min_table_cols: int = 0


FUNCTIONAL_QUALITY_MATRIX = (
    FunctionalQualityCase(
        fixture_id="generated-simple-paragraph",
        engine="tesseract",
        profile="backend_raw",
        min_token_recall=0.8,
    ),
    FunctionalQualityCase(
        fixture_id="generated-product-table",
        engine="tesseract",
        profile="backend_tesseract_standard",
        min_token_recall=0.85,
        min_pair_recall=0.5,
        min_digit_recall=0.6,
        min_table_rows=3,
        min_table_cols=3,
    ),
    FunctionalQualityCase(
        fixture_id="generated-low-contrast-noise",
        engine="tesseract",
        profile="backend_raw",
        min_token_recall=0.75,
    ),
    FunctionalQualityCase(
        fixture_id="generated-small-skew",
        engine="auto",
        profile="backend_auto_standard",
        min_token_recall=0.75,
    ),
    FunctionalQualityCase(
        fixture_id="generated-tiny-score-list",
        engine="tesseract",
        profile="backend_tesseract_standard",
        min_token_recall=0.85,
        min_pair_recall=0.75,
        min_digit_recall=0.8,
    ),
)


def _compact(text: str) -> str:
    return "".join(text.split())


def _require_tesseract_languages(required=("eng", "rus")):
    installed = set(TesseractEngine.installed_languages())
    missing = set(required) - installed
    if missing:
        pytest.skip(
            "Functional OCR quality requires Tesseract languages "
            f"{sorted(required)}; missing {sorted(missing)}. Install tesseract and language packs."
        )


def _require_strict_tesseract_languages():
    _require_tesseract_languages(("eng", "rus", "chi_sim"))


def _assert_tokens(markdown: str, source: str):
    compact = _compact(markdown)
    missing = [token for token in EXPECTED_TOKENS if token not in compact]
    assert (
        not missing
    ), f"{source} missed tokens {missing}\nExpected source text:\n{QUALITY_TEXT}\nOCR output:\n{markdown}"


def _convert_generated_fixture(case: FunctionalQualityCase):
    spec = functional_ocr_fixture_spec(case.fixture_id)
    payload = functional_ocr_fixture_bytes(case.fixture_id)
    profile = resolve_pipeline_profile(case.engine, case.profile)
    events = list(
        convert_service.iter_convert_bytes(
            payload,
            filename=f"{spec.id}.png",
            engine_type=case.engine,
            pipeline_profile=profile,
        )
    )
    pages = [event for event in events if event["type"] == "page"]
    complete = next(event for event in events if event["type"] == "complete")
    markdown = "\n\n---\n\n".join(event["markdown"] for event in pages)
    return spec, markdown, complete["meta"]


@pytest.mark.parametrize(
    "case",
    FUNCTIONAL_QUALITY_MATRIX,
    ids=lambda case: f"{case.fixture_id}:{case.engine}:{case.profile}",
)
def test_generated_functional_ocr_quality_matrix(case: FunctionalQualityCase):
    _require_tesseract_languages(("eng",))
    if case.min_table_rows or case.min_table_cols:
        pytest.importorskip(
            "cv2",
            reason="Table quality cases require OpenCV layout analysis to emit markdown table markers.",
        )

    spec, markdown, meta = _convert_generated_fixture(case)

    assert spec in FUNCTIONAL_OCR_FIXTURE_REGISTRY
    assert spec.tier == "quality"
    assert meta["engine"] in {"tesseract", "easyocr"}
    assert meta["pages"] == case.expected_pages
    assert meta["pipeline"] == case.profile
    assert markdown.strip(), f"{spec.id} produced empty markdown with meta {meta}"

    recall = token_recall(list(spec.expected_tokens), markdown)
    assert recall >= case.min_token_recall, (
        f"{spec.id} token recall {recall:.2f} < {case.min_token_recall:.2f}; "
        f"missing {missing_tokens(list(spec.expected_tokens), markdown)}\nOCR output:\n{markdown}"
    )

    if spec.expected_pairs:
        pair_recall = name_value_pair_recall(list(spec.expected_pairs), markdown)
        assert pair_recall >= case.min_pair_recall, (
            f"{spec.id} pair recall {pair_recall:.2f} < {case.min_pair_recall:.2f}; "
            f"expected {spec.expected_pairs}\nOCR output:\n{markdown}"
        )
        expected_values = "\n".join(" ".join(pair) for pair in spec.expected_pairs)
        assert digit_sequence_recall(expected_values, markdown) >= case.min_digit_recall

    if case.min_table_rows or case.min_table_cols:
        rows, cols = markdown_table_shape(markdown)
        assert rows >= case.min_table_rows and cols >= case.min_table_cols, (
            f"{spec.id} expected at least {case.min_table_rows}x{case.min_table_cols} markdown table, "
            f"got {rows}x{cols}\nOCR output:\n{markdown}"
        )
        assert "|" in markdown


@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("multilingual.png", "image/png"),
        ("multilingual.jpg", "image/jpeg"),
        ("multilingual.webp", "image/webp"),
        ("multilingual.pdf", "application/pdf"),
    ],
)
def test_backend_tesseract_strict_multilingual_quality(filename: str, mime: str):
    _require_strict_tesseract_languages()
    fixture_dir = write_quality_fixtures()
    fixture = fixture_dir / filename

    with fixture.open("rb") as file_obj:
        markdown, _ = convert_service.convert_bytes(
            file_obj.read(),
            filename=filename,
            engine_type="tesseract",
            pipeline_profile=resolve_pipeline_profile("tesseract"),
        )

    _assert_tokens(markdown, filename)


@pytest.mark.parametrize("template_index", range(10))
def test_backend_tesseract_generated_document_quality(template_index: int):
    _require_strict_tesseract_languages()
    templates = generate_document_templates()
    template = templates[template_index]
    try:
        from io import BytesIO

        payload = BytesIO()
        template.image.save(payload, format="PNG")
        markdown, _ = convert_service.convert_bytes(
            payload.getvalue(),
            filename=f"{template.name}.png",
            engine_type="tesseract",
            pipeline_profile=resolve_pipeline_profile("tesseract", "backend_raw"),
        )
    finally:
        for generated in templates:
            generated.image.close()

    assert ordered_phrase_recall(list(template.expected_phrases), markdown) >= 0.6
    if template.expected_pairs:
        assert name_value_pair_recall(list(template.expected_pairs), markdown) >= 0.5
        expected = "\n".join(" ".join(pair) for pair in template.expected_pairs)
        assert digit_sequence_recall(expected, markdown) >= 0.6


def test_backend_tesseract_long_cart_keeps_top_middle_and_bottom():
    _require_strict_tesseract_languages()
    template = generate_long_cart()
    try:
        from io import BytesIO

        payload = BytesIO()
        template.image.save(payload, format="PNG")
        markdown, meta = convert_service.convert_bytes(
            payload.getvalue(),
            filename="long-cart.png",
            engine_type="tesseract",
            pipeline_profile=resolve_pipeline_profile("tesseract", "backend_raw"),
        )
    finally:
        template.image.close()

    assert ordered_phrase_recall(list(template.expected_phrases), markdown) >= 0.8
    assert name_value_pair_recall(list(template.expected_pairs), markdown) >= 2 / 3
    assert meta["chunks"] > 1
