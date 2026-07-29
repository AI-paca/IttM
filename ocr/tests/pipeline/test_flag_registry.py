from app.pipeline_config import OCR_PIPELINE_PROFILES
from app.pipeline_core.flag_registry import input_flag_registry, profile_field_value
from app.pipeline_flags import profile_flag_items


def test_every_published_profile_flag_has_exactly_one_stage_owner():
    published = {
        flag.key
        for profile in OCR_PIPELINE_PROFILES.values()
        for flag in profile_flag_items(profile)
    }
    registry = input_flag_registry()

    assert set(registry) == published
    assert all(registration.owner for registration in registry.values())
    assert all(registration.field for registration in registry.values())


def test_every_registry_field_resolves_against_its_profile_source():
    registry = input_flag_registry()

    for profile in OCR_PIPELINE_PROFILES.values():
        published = {flag.key for flag in profile_flag_items(profile)}
        for key in published:
            profile_field_value(profile, registry[key])


def test_repeated_and_nested_flags_declare_the_real_profile_fields():
    registry = input_flag_registry()

    assert registry["preprocess"].field == "image_preprocessing"
    assert registry["layout_stage"].field == "layout.allowed_stages"
    assert registry["layout_param:max_depth"].field == "layout.default_parameters[max_depth]"
    assert registry["edge_word_fallback_psm"].field == "edge_word_fallback_psms"
    assert registry["table_word_formatter"].field == "table_word_formatters"
    assert registry["lexical_correction"].status == "legacy_coupled_language_retry"


def test_trusted_api_bypasses_tesseract_specific_flags():
    registry = input_flag_registry()

    for key in (
        "ocr_language_retry",
        "lexical_correction",
        "recursive_table_cell_ocr",
        "table_raw_text_fallback",
        "dense_grid_fallback",
    ):
        assert registry[key].trusted_api == "bypass"


def test_only_structure_and_render_flags_may_process_plain_api_text():
    registry = input_flag_registry()
    allowed = {
        key
        for key, registration in registry.items()
        if registration.trusted_api == "plain_text_only"
    }

    assert allowed == {
        "contextual_markdown_grammar",
        "structural_output",
        "table_layout_normalization",
        "table_min_cell_coverage",
        "table_min_word_cell_coverage",
        "table_slot_builder",
        "table_word_formatter",
        "wide_table_min_word_cell_coverage",
    }
