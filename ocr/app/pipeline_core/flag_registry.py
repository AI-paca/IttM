from __future__ import annotations

from dataclasses import dataclass

from app.pipeline_config import OcrPipelineProfile


@dataclass(frozen=True)
class InputFlagRegistration:
    key: str
    owner: str
    field: str
    trusted_api: str = "bypass"
    status: str = "active"


PROFILE_FIELD_BY_FLAG = {
    "preprocess": "image_preprocessing",
    "layout_selector": "layout.selector",
    "layout_stage": "layout.allowed_stages",
    "ocr_language_priority": "tesseract_language_priority",
    "ocr_text_region_psm": "text_region_psm",
    "ocr_document_region_psm": "document_region_psm",
    "ocr_wide_text_region_psm": "wide_text_region_psm",
    "ocr_table_word_psm": "table_word_psm",
    "ocr_large_table_word_psm": "large_table_word_psm",
    "edge_word_fallback_psm": "edge_word_fallback_psms",
    "table_word_formatter": "table_word_formatters",
}


def _profile_field(key: str) -> str:
    if key.startswith("layout_param:"):
        return f"layout.default_parameters[{key.split(':', 1)[1]}]"
    return PROFILE_FIELD_BY_FLAG.get(key, key)


def _registrations(
    owner: str,
    keys: tuple[str, ...],
    *,
    trusted_api: str = "bypass",
    statuses: dict[str, str] | None = None,
) -> tuple[InputFlagRegistration, ...]:
    return tuple(
        InputFlagRegistration(
            key=key,
            owner=owner,
            field=_profile_field(key),
            trusted_api=trusted_api,
            status=(statuses or {}).get(key, "active"),
        )
        for key in keys
    )


INPUT_FLAG_REGISTRY = (
    *_registrations(
        "align",
        (
            "preprocess",
            "ocr_border_pixels",
            "dense_grid_target_width",
        ),
    ),
    *_registrations(
        "segment",
        (
            "layout_selector",
            "layout_stage",
            "layout_param:chunk_overlap",
            "layout_param:direct_region_ocr",
            "layout_param:max_depth",
            "layout_param:max_region_height",
            "layout_param:min_cell_height",
            "layout_param:min_region_height",
            "layout_param:min_region_width",
            "layout_param:min_separator_coverage",
            "layout_param:min_separator_gap",
            "layout_param:region_deskew",
            "layout_param:region_page_dewarp",
            "grid_min_confirmed_cell_ratio",
        ),
    ),
    *_registrations(
        "recognize_segments",
        (
            "ocr_language_priority",
            "ocr_text_region_psm",
            "ocr_document_region_psm",
            "ocr_wide_text_region_psm",
            "ocr_table_word_psm",
            "ocr_large_table_word_psm",
            "table_raw_text_fallback",
            "table_raw_text_fallback_psm",
            "table_raw_text_fallback_min_rows",
            "table_raw_text_fallback_min_cols",
            "table_raw_text_fallback_max_cols",
            "table_raw_text_fallback_min_ratio",
            "sparse_text_fallback_engine",
            "sparse_text_fallback_min_tokens",
            "sparse_text_fallback_min_ratio",
            "recursive_table_cell_ocr",
            "recursive_table_cell_ocr_batch_pixels",
            "max_table_cell_ocr_calls",
            "table_word_recognition",
        ),
    ),
    *_registrations(
        "select_language_candidate",
        ("ocr_language_retry",),
    ),
    *_registrations(
        "lexical_correction",
        ("lexical_correction",),
        statuses={"lexical_correction": "legacy_coupled_language_retry"},
    ),
    *_registrations(
        "group_structures",
        (
            "table_layout_normalization",
            "table_slot_builder",
            "table_word_formatter",
            "table_min_cell_coverage",
            "table_min_word_cell_coverage",
            "wide_table_min_word_cell_coverage",
        ),
        trusted_api="plain_text_only",
    ),
    *_registrations(
        "fallback",
        (
            "dense_grid_fallback",
            "spatial_full_page_fallback",
            "dark_ui_text_fallback",
            "edge_word_fallback_psm",
            "edge_word_fallback_min_tokens",
        ),
    ),
    *_registrations(
        "render_markdown",
        (
            "contextual_markdown_grammar",
            "structural_output",
        ),
        trusted_api="plain_text_only",
    ),
)


def input_flag_registry() -> dict[str, InputFlagRegistration]:
    result: dict[str, InputFlagRegistration] = {}
    for registration in INPUT_FLAG_REGISTRY:
        if registration.key in result:
            raise ValueError(f"Duplicate input flag owner for '{registration.key}'")
        result[registration.key] = registration
    return result


def profile_field_value(
    profile: OcrPipelineProfile,
    registration: InputFlagRegistration,
):
    """Resolve the declared source field, including repeated tuple/map flags."""

    field = registration.field
    if field.startswith("layout.default_parameters["):
        parameter = field.removeprefix("layout.default_parameters[").removesuffix("]")
        return dict(profile.layout.default_parameters).get(parameter)
    value = profile
    for part in field.split("."):
        value = getattr(value, part)
    return value
