from dataclasses import dataclass, replace

from app.layout.contracts import FeatureValue


@dataclass(frozen=True)
class LayoutPipelineConfig:
    feature_extractors: tuple[str, ...] = ()
    selector: str = "fixed"
    allowed_stages: tuple[str, ...] = ()
    default_parameters: tuple[tuple[str, FeatureValue], ...] = ()


@dataclass(frozen=True)
class OcrPipelineProfile:
    name: str
    tesseract_language_priority: tuple[str, ...] = (
        "rus",
        "eng",
        "kaz",
        "kir",
        "chi_sim",
    )
    text_region_psm: int = 6
    document_region_psm: int = 3
    wide_text_region_psm: int = 11
    table_word_psm: int = 6
    large_table_word_psm: int = 11
    table_raw_text_fallback: bool = False
    table_raw_text_fallback_psm: int = 11
    table_raw_text_fallback_min_rows: int = 10
    table_raw_text_fallback_min_cols: int = 8
    table_raw_text_fallback_max_cols: int = 14
    table_raw_text_fallback_min_ratio: float = 0.75
    sparse_text_fallback_engine: str | None = None
    sparse_text_fallback_min_tokens: int = 18
    sparse_text_fallback_min_ratio: float = 1.25
    dense_grid_fallback: bool = False
    spatial_full_page_fallback: bool = False
    dark_ui_text_fallback: bool = False
    contextual_markdown_grammar: bool = False
    dense_grid_target_width: int = 3300
    ocr_border_pixels: int = 10
    edge_word_fallback_psms: tuple[int, ...] = (8, 13)
    edge_word_fallback_min_tokens: int = 1
    image_preprocessing: tuple[str, ...] = ()
    layout: LayoutPipelineConfig = LayoutPipelineConfig()
    grid_min_confirmed_cell_ratio: float = 0.0
    table_min_word_cell_coverage: float = 0.35
    wide_table_min_word_cell_coverage: float = 0.02
    table_min_cell_coverage: float = 0.5
    max_table_cell_ocr_calls: int = 16
    table_layout_normalization: str = "logical_columns"
    table_slot_builder: str = "off"
    table_word_recognition: str = "bounded_tiles"
    table_word_formatters: tuple[str, ...] = ("generic_markdown",)
    lexical_correction: str = "off"
    ocr_language_retry: str = "off"
    recursive_table_cell_ocr: str = "auto"
    recursive_table_cell_ocr_batch_pixels: int = 8_000_000
    structural_output: str = "markdown"

    @property
    def layout_analysis(self) -> tuple[str, ...]:
        return self.layout.allowed_stages


_UNIVERSAL_RECURSIVE_GRID_LAYOUT = LayoutPipelineConfig(
    selector="fixed",
    allowed_stages=("recursive_grid",),
    default_parameters=(
        ("max_region_height", 1400),
        ("min_region_height", 120),
        ("min_cell_height", 24),
        ("min_region_width", 80),
        ("min_separator_gap", 8),
        ("chunk_overlap", 16),
        ("max_depth", 32),
        ("region_page_dewarp", True),
        ("region_deskew", True),
    ),
)


OCR_PIPELINE_PROFILES: dict[str, OcrPipelineProfile] = {
    "backend_auto_standard": OcrPipelineProfile(
        name="backend_auto_standard",
        table_raw_text_fallback=True,
        dense_grid_fallback=True,
        spatial_full_page_fallback=True,
        dark_ui_text_fallback=True,
        contextual_markdown_grammar=True,
        lexical_correction="t9_small",
        ocr_language_retry="t9_small",
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=_UNIVERSAL_RECURSIVE_GRID_LAYOUT,
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_tesseract_standard": OcrPipelineProfile(
        name="backend_tesseract_standard",
        table_raw_text_fallback=True,
        dense_grid_fallback=True,
        spatial_full_page_fallback=True,
        dark_ui_text_fallback=True,
        contextual_markdown_grammar=False,
        lexical_correction="t9_small",
        ocr_language_retry="t9_small",
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=_UNIVERSAL_RECURSIVE_GRID_LAYOUT,
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_easyocr_standard": OcrPipelineProfile(
        name="backend_easyocr_standard",
        table_raw_text_fallback=True,
        table_raw_text_fallback_max_cols=30,
        sparse_text_fallback_engine="tesseract",
        dense_grid_fallback=True,
        spatial_full_page_fallback=True,
        dark_ui_text_fallback=True,
        contextual_markdown_grammar=True,
        lexical_correction="t9_small",
        ocr_language_retry="t9_small",
        edge_word_fallback_min_tokens=1,
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=_UNIVERSAL_RECURSIVE_GRID_LAYOUT,
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_easyocr_table": OcrPipelineProfile(
        name="backend_easyocr_table",
        image_preprocessing=("projected_document_dewarp",),
        layout=LayoutPipelineConfig(
            allowed_stages=("table_regions",),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_easyocr_spatial": OcrPipelineProfile(
        name="backend_easyocr_spatial",
        image_preprocessing=("projected_document_dewarp",),
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("direct_region_ocr", True),
                ("max_region_height", 2800),
                ("min_region_height", 300),
                ("min_separator_coverage", 0.55),
            ),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_curriculum": OcrPipelineProfile(
        name="backend_curriculum",
        dense_grid_fallback=True,
        spatial_full_page_fallback=True,
        dark_ui_text_fallback=True,
        contextual_markdown_grammar=True,
        lexical_correction="t9_small",
        ocr_language_retry="t9_small",
        image_preprocessing=("projected_document_dewarp",),
        layout=_UNIVERSAL_RECURSIVE_GRID_LAYOUT,
        grid_min_confirmed_cell_ratio=0.35,
        table_layout_normalization="preserve_grid",
        table_word_recognition="single_pass_with_left_strip",
        table_word_formatters=("curriculum", "generic_markdown"),
    ),
    "backend_plain_text": OcrPipelineProfile(
        name="backend_plain_text",
        image_preprocessing=("projected_document_dewarp",),
    ),
    "backend_raw": OcrPipelineProfile(
        name="backend_raw",
        image_preprocessing=(),
    ),
}

_TABLE_FIRST_LAYOUT = LayoutPipelineConfig(
    feature_extractors=("projection_geometry",),
    selector="table_first_heuristic_v1",
    allowed_stages=("table_regions", "spatial_regions"),
    default_parameters=(
        ("max_region_height", 1400),
        ("min_region_height", 300),
        ("min_separator_coverage", 0.55),
    ),
)

OCR_PIPELINE_PROFILES.update(
    {
        "backend_auto_table_first": replace(
            OCR_PIPELINE_PROFILES["backend_auto_standard"],
            name="backend_auto_table_first",
            layout=_TABLE_FIRST_LAYOUT,
        ),
        "backend_tesseract_table_first": replace(
            OCR_PIPELINE_PROFILES["backend_tesseract_standard"],
            name="backend_tesseract_table_first",
            layout=_TABLE_FIRST_LAYOUT,
        ),
        "backend_tesseract_table_slots": replace(
            OCR_PIPELINE_PROFILES["backend_tesseract_standard"],
            name="backend_tesseract_table_slots",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="line_merge_v1",
        ),
        "backend_tesseract_recursive_slots": replace(
            OCR_PIPELINE_PROFILES["backend_tesseract_standard"],
            name="backend_tesseract_recursive_slots",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="recursive_gaps_v1",
        ),
        "backend_tesseract_recursive_slots_t9": replace(
            OCR_PIPELINE_PROFILES["backend_tesseract_standard"],
            name="backend_tesseract_recursive_slots_t9",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="recursive_gaps_v1",
            lexical_correction="t9_small",
            ocr_language_retry="t9_small",
        ),
        "backend_easyocr_table_first": replace(
            OCR_PIPELINE_PROFILES["backend_easyocr_standard"],
            name="backend_easyocr_table_first",
            layout=_TABLE_FIRST_LAYOUT,
        ),
        "backend_easyocr_table_slots": replace(
            OCR_PIPELINE_PROFILES["backend_easyocr_standard"],
            name="backend_easyocr_table_slots",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="line_merge_v1",
        ),
        "backend_easyocr_recursive_slots": replace(
            OCR_PIPELINE_PROFILES["backend_easyocr_standard"],
            name="backend_easyocr_recursive_slots",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="recursive_gaps_v1",
        ),
        "backend_easyocr_recursive_slots_t9": replace(
            OCR_PIPELINE_PROFILES["backend_easyocr_standard"],
            name="backend_easyocr_recursive_slots_t9",
            layout=_TABLE_FIRST_LAYOUT,
            table_slot_builder="recursive_gaps_v1",
            lexical_correction="t9_small",
            ocr_language_retry="t9_small",
        ),
        "backend_tesseract_greek_math": replace(
            OCR_PIPELINE_PROFILES["backend_tesseract_standard"],
            name="backend_tesseract_greek_math",
            tesseract_language_priority=(
                "rus",
                "eng",
                "kaz",
                "kir",
                "chi_sim",
                "ell",
                "equ",
            ),
        ),
    }
)

DEFAULT_ENGINE_PIPELINE_PROFILES = {
    "auto": "backend_auto_standard",
    "tesseract": "backend_tesseract_standard",
    "easyocr": "backend_easyocr_standard",
}


def resolve_pipeline_profile(engine_type: str, profile_name: str | None = None) -> OcrPipelineProfile:
    name = profile_name or DEFAULT_ENGINE_PIPELINE_PROFILES.get(engine_type, "backend_auto_standard")
    profile = OCR_PIPELINE_PROFILES.get(name)
    if profile is None:
        known_profiles = ", ".join(sorted(OCR_PIPELINE_PROFILES))
        raise ValueError(f"Unknown OCR pipeline profile '{name}'. Known profiles: {known_profiles}")
    return profile


def with_lexical_correction(profile: OcrPipelineProfile, mode: str) -> OcrPipelineProfile:
    return replace(
        profile,
        lexical_correction=mode,
        ocr_language_retry=mode,
    )


def with_ocr_language_retry(profile: OcrPipelineProfile, mode: str) -> OcrPipelineProfile:
    return replace(profile, ocr_language_retry=mode)


def with_table_slot_builder(profile: OcrPipelineProfile, mode: str) -> OcrPipelineProfile:
    return replace(profile, table_slot_builder=mode)


def with_recursive_table_cell_ocr(
    profile: OcrPipelineProfile,
    mode: str,
) -> OcrPipelineProfile:
    return replace(profile, recursive_table_cell_ocr=mode)


def with_structural_output(
    profile: OcrPipelineProfile,
    mode: str,
) -> OcrPipelineProfile:
    return replace(profile, structural_output=mode)
