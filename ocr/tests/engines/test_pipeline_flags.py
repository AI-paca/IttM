import pytest

from app.pipeline_config import resolve_pipeline_profile
from app.pipeline_flags import (
    apply_pipeline_flag_overrides,
    ensure_flag_overrides_allowed,
    pipeline_flag_catalog,
    pipeline_flags_payload,
    profile_flags,
)


def test_profile_flags_are_effective_runtime_strings():
    profile = resolve_pipeline_profile("easyocr")
    flags = profile_flags(profile)

    assert "preprocess:projector_slide_dewarp" in flags
    assert "layout_selector:fixed" in flags
    assert "layout_stage:recursive_grid" in flags
    assert "layout_param:min_separator_gap=8" in flags
    assert "table_raw_text_fallback:True" in flags
    assert "table_raw_text_fallback_min_ratio:0.75" in flags
    assert "sparse_text_fallback_engine:tesseract" in flags
    assert "dense_grid_fallback:True" in flags
    assert "spatial_full_page_fallback:True" in flags
    assert "dark_ui_text_fallback:True" in flags
    assert "contextual_markdown_grammar:True" in flags
    assert "dense_grid_target_width:3300" in flags
    assert "ocr_border_pixels:10" in flags
    assert "edge_word_fallback_psm:8" in flags
    assert "edge_word_fallback_psm:13" in flags
    assert "edge_word_fallback_min_tokens:1" in flags
    assert "lexical_correction:t9_small" in flags
    assert "ocr_language_retry:t9_small" in flags
    assert "recursive_table_cell_ocr:auto" in flags
    assert "recursive_table_cell_ocr_batch_pixels:8000000" in flags
    assert "table_slot_builder:off" in flags


def test_table_first_profile_flags_are_explicitly_opt_in():
    flags = profile_flags(resolve_pipeline_profile("tesseract", "backend_tesseract_table_first"))

    assert "layout_selector:table_first_heuristic_v1" in flags
    assert "layout_stage:table_regions" in flags
    assert "layout_stage:spatial_regions" in flags


def test_standard_backend_profiles_share_table_composition_fallback():
    tesseract_flags = profile_flags(resolve_pipeline_profile("tesseract"))
    easyocr_flags = profile_flags(resolve_pipeline_profile("easyocr"))

    assert "table_raw_text_fallback:True" in tesseract_flags
    assert "table_raw_text_fallback:True" in easyocr_flags
    assert "dense_grid_fallback:True" in tesseract_flags
    assert "dense_grid_fallback:True" in easyocr_flags
    assert "spatial_full_page_fallback:True" in tesseract_flags
    assert "spatial_full_page_fallback:True" in easyocr_flags
    assert "dark_ui_text_fallback:True" in tesseract_flags
    assert "dark_ui_text_fallback:True" in easyocr_flags
    assert "contextual_markdown_grammar:False" in tesseract_flags
    assert "contextual_markdown_grammar:True" in easyocr_flags
    assert "sparse_text_fallback_engine:tesseract" in easyocr_flags
    assert "lexical_correction:t9_small" in tesseract_flags
    assert "ocr_language_retry:t9_small" in tesseract_flags
    assert "lexical_correction:t9_small" in easyocr_flags
    assert "ocr_language_retry:t9_small" in easyocr_flags


def test_pipeline_flag_catalog_includes_api_contract_keys():
    keys = {entry["key"] for entry in pipeline_flag_catalog()}

    assert "pipeline_flags" in keys
    assert "overrides_enabled" in keys
    assert "preprocess_runtime" in keys
    assert "ocr_runtime" in keys
    assert "browser_profile_reason" in keys
    assert "ocr_text_region_psm" in keys
    assert "lexical_correction" in keys
    assert "ocr_language_retry" in keys
    assert "recursive_table_cell_ocr" in keys
    assert "recursive_table_cell_ocr_batch_pixels" in keys
    assert "table_slot_builder" in keys


def test_pipeline_flags_payload_exposes_profiles_and_disabled_overrides():
    payload = pipeline_flags_payload()

    assert payload["overrides_enabled"] is False
    assert payload["override_parameter"] == "pipeline_flags"
    supported = {entry["key"]: entry for entry in payload["supported_overrides"]}
    assert supported["structural_output"]["status"] == "active"
    assert supported["lexical_correction"]["status"] == "legacy_coupled_language_retry"
    assert "backend_easyocr_standard" in payload["profiles"]


def test_combined_language_overrides_have_stable_stage_order():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "ocr_language_retry:off;lexical_correction:t9_small",
    )

    assert profile.lexical_correction == "t9_small"
    assert profile.ocr_language_retry == "off"


def test_pipeline_flag_overrides_fail_closed(monkeypatch):
    monkeypatch.delenv("OCR_PIPELINE_FLAG_OVERRIDES", raising=False)

    with pytest.raises(ValueError, match="disabled"):
        ensure_flag_overrides_allowed("ocr_text_region_psm:11")


def test_lexical_correction_pipeline_flag_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "lexical_correction:t9_small",
    )

    assert profile.lexical_correction == "t9_small"
    assert profile.ocr_language_retry == "t9_small"
    assert "lexical_correction:t9_small" in profile_flags(profile)
    assert "ocr_language_retry:t9_small" in profile_flags(profile)


def test_ocr_language_retry_pipeline_flag_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "ocr_language_retry:t9_small",
    )

    assert profile.ocr_language_retry == "t9_small"
    assert "ocr_language_retry:t9_small" in profile_flags(profile)


def test_table_slot_builder_pipeline_flag_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "table_slot_builder:line_merge_v1",
    )

    assert profile.table_slot_builder == "line_merge_v1"
    assert "table_slot_builder:line_merge_v1" in profile_flags(profile)


def test_recursive_table_cell_ocr_pipeline_flag_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "recursive_table_cell_ocr:off",
    )

    assert profile.recursive_table_cell_ocr == "off"
    assert "recursive_table_cell_ocr:off" in profile_flags(profile)


def test_structural_records_pipeline_flag_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "structural_output:records",
    )

    assert profile.structural_output == "records"
    assert "structural_output:records" in profile_flags(profile)


def test_recursive_gap_table_slot_builder_profile_is_explicit():
    profile = resolve_pipeline_profile("tesseract", "backend_tesseract_recursive_slots")

    assert profile.table_slot_builder == "recursive_gaps_v1"
    assert "table_slot_builder:recursive_gaps_v1" in profile_flags(profile)


def test_recursive_gap_t9_profile_is_explicit():
    profile = resolve_pipeline_profile("tesseract", "backend_tesseract_recursive_slots_t9")

    assert profile.table_slot_builder == "recursive_gaps_v1"
    assert profile.lexical_correction == "t9_small"
    assert profile.ocr_language_retry == "t9_small"
    flags = profile_flags(profile)
    assert "table_slot_builder:recursive_gaps_v1" in flags
    assert "lexical_correction:t9_small" in flags
    assert "ocr_language_retry:t9_small" in flags


def test_recursive_gap_table_slot_builder_override_is_allowed():
    profile = apply_pipeline_flag_overrides(
        resolve_pipeline_profile("tesseract"),
        "table_slot_builder:recursive_gaps_v1",
    )

    assert profile.table_slot_builder == "recursive_gaps_v1"
    assert "table_slot_builder:recursive_gaps_v1" in profile_flags(profile)
