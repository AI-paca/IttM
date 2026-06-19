import pytest

from app.pipeline_config import resolve_pipeline_profile
from app.pipeline_flags import (
    ensure_flag_overrides_allowed,
    pipeline_flag_catalog,
    pipeline_flags_payload,
    profile_flags,
)


def test_profile_flags_are_effective_runtime_strings():
    profile = resolve_pipeline_profile("easyocr")
    flags = profile_flags(profile)

    assert "preprocess:projector_slide_dewarp" in flags
    assert "layout_selector:uniform_spatial_v1" in flags
    assert "table_raw_text_fallback:True" in flags
    assert "table_raw_text_fallback_min_ratio:0.75" in flags
    assert "sparse_text_fallback_engine:tesseract" in flags
    assert "dense_grid_fallback:True" in flags
    assert "dense_grid_target_width:3300" in flags
    assert "ocr_border_pixels:10" in flags
    assert "edge_word_fallback_psm:8" in flags
    assert "edge_word_fallback_psm:13" in flags
    assert "edge_word_fallback_min_tokens:1" in flags


def test_standard_backend_profiles_share_table_composition_fallback():
    tesseract_flags = profile_flags(resolve_pipeline_profile("tesseract"))
    easyocr_flags = profile_flags(resolve_pipeline_profile("easyocr"))

    assert "table_raw_text_fallback:True" in tesseract_flags
    assert "table_raw_text_fallback:True" in easyocr_flags
    assert "dense_grid_fallback:True" in tesseract_flags
    assert "dense_grid_fallback:True" in easyocr_flags
    assert "sparse_text_fallback_engine:tesseract" in easyocr_flags


def test_pipeline_flag_catalog_includes_api_contract_keys():
    keys = {entry["key"] for entry in pipeline_flag_catalog()}

    assert "pipeline_flags" in keys
    assert "overrides_enabled" in keys
    assert "preprocess_runtime" in keys
    assert "ocr_runtime" in keys
    assert "browser_profile_reason" in keys
    assert "ocr_text_region_psm" in keys


def test_pipeline_flags_payload_exposes_profiles_and_disabled_overrides():
    payload = pipeline_flags_payload()

    assert payload["overrides_enabled"] is False
    assert payload["override_parameter"] == "pipeline_flags"
    assert "backend_easyocr_standard" in payload["profiles"]


def test_pipeline_flag_overrides_fail_closed(monkeypatch):
    monkeypatch.delenv("OCR_PIPELINE_FLAG_OVERRIDES", raising=False)

    with pytest.raises(ValueError, match="disabled"):
        ensure_flag_overrides_allowed("ocr_text_region_psm:11")
