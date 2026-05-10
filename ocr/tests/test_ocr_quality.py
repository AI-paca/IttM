import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.engines.tesseract_engine import TesseractEngine
from app.main import app
from tests.quality_fixtures import QUALITY_TEXT, write_quality_fixtures

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OCR_QUALITY") != "1",
    reason="OCR quality tests are heavy; set RUN_OCR_QUALITY=1 in debug.sh or GitHub Actions OCR job.",
)

client = TestClient(app)
EXPECTED_TOKENS = [
    "ABCXYZ",
    "abcxyz",
    "0123456789",
    "РУССКИЙ",
    "АБВГДЕЖЗ",
    "абвгдежз",
    "中文测试",
    "汉字识别",
    "MIXEDABCД12345中文",
    "12345",
]


def _compact(text: str) -> str:
    return "".join(text.split())


def _assert_tesseract_languages():
    installed = set(TesseractEngine.installed_languages())
    required = {"eng", "rus", "chi_sim"}
    missing = required - installed
    assert not missing, (
        "Strict OCR quality requires Tesseract languages "
        f"{sorted(required)}; missing {sorted(missing)}. Install tesseract-ocr-rus and tesseract-ocr-chi-sim."
    )


def _assert_tokens(markdown: str, source: str):
    compact = _compact(markdown)
    missing = [token for token in EXPECTED_TOKENS if token not in compact]
    assert (
        not missing
    ), f"{source} missed tokens {missing}\nExpected source text:\n{QUALITY_TEXT}\nOCR output:\n{markdown}"


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
    _assert_tesseract_languages()
    fixture_dir = write_quality_fixtures()
    fixture = fixture_dir / filename

    with fixture.open("rb") as file_obj:
        response = client.post(
            "/convert?engine_type=tesseract",
            files={"file": (filename, file_obj, mime)},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_tokens(payload["markdown"], filename)
