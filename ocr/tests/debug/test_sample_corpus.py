import importlib.util
from pathlib import Path

import pytest

from app.engines.tesseract_engine import TesseractEngine
from app.services import convert_service
from tests.support.quality_metrics import markdown_table_shape

REPO_ROOT = Path(__file__).resolve().parents[3]
DEBUG_INPUTS = REPO_ROOT / "debug" / "fixtures"
DEBUG_EXPECTED = REPO_ROOT / "debug" / "reference"
REPORT_PATH = REPO_ROOT / "scripts" / "debug" / "debug_report.py"


def _load_debug_report():
    spec = importlib.util.spec_from_file_location("debug_report", REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _require_tesseract():
    pytest.importorskip("pytesseract")
    engine = TesseractEngine()
    if not engine.available():
        pytest.skip("Tesseract binary is not available")


def _match_score(actual: str, fixture_name: str):
    debug_report = _load_debug_report()
    expected = (DEBUG_EXPECTED / f"{fixture_name}.md").read_text(
        encoding="utf-8",
        errors="replace",
    )
    return debug_report.scored_expected_match(actual, expected)


def _assert_sample_quality_gate(actual: str, fixture_name: str):
    score = _match_score(actual, fixture_name)
    assert float(score.match_percent) >= 90.0
    assert score.compact_quality_gate == "pass"
    assert score.lexical_t9_gate == "pass"
    assert score.success_probability_gate == "pass"
    return score


def test_tracked_4k_sample_reaches_default_tesseract_gate():
    _require_tesseract()
    fixture = DEBUG_INPUTS / "SAMPLE_4k.png"
    assert fixture.exists()

    markdown, meta = convert_service.convert_bytes(
        fixture.read_bytes(),
        filename=fixture.name,
        engine_type="tesseract",
    )

    _assert_sample_quality_gate(markdown, fixture.name)
    assert meta["engine"] == "tesseract"


def test_tracked_hard_mixed_image_pdf_reaches_gate_and_keeps_10x14_table():
    pytest.importorskip("cv2")
    _require_tesseract()
    fixture = DEBUG_INPUTS / "SAMPLE_mixed_ru_en_zh_table_image.pdf"
    assert fixture.exists()
    content = fixture.read_bytes()

    assert convert_service._extract_pdf_text_layer_pages(content, fixture.name) == []
    markdown, meta = convert_service.convert_bytes(
        content,
        filename=fixture.name,
        engine_type="tesseract",
    )
    table_rows, table_cols = markdown_table_shape(markdown)

    score = _assert_sample_quality_gate(markdown, fixture.name)
    assert score.markdown_grammar_gate == "pass"
    assert table_rows >= 14
    assert table_cols >= 10
    assert meta["tables_found"] >= 1
