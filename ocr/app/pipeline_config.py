from dataclasses import dataclass

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
    table_word_recognition: str = "bounded_tiles"
    table_word_formatters: tuple[str, ...] = ("generic_markdown",)

    @property
    def layout_analysis(self) -> tuple[str, ...]:
        return self.layout.allowed_stages


OCR_PIPELINE_PROFILES: dict[str, OcrPipelineProfile] = {
    "backend_auto_standard": OcrPipelineProfile(
        name="backend_auto_standard",
        table_raw_text_fallback=True,
        dense_grid_fallback=True,
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 1400),
                ("min_region_height", 300),
                ("min_separator_coverage", 0.55),
            ),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_tesseract_standard": OcrPipelineProfile(
        name="backend_tesseract_standard",
        table_raw_text_fallback=True,
        dense_grid_fallback=True,
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 1400),
                ("min_region_height", 300),
                ("min_separator_coverage", 0.55),
            ),
        ),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_easyocr_standard": OcrPipelineProfile(
        name="backend_easyocr_standard",
        table_raw_text_fallback=True,
        table_raw_text_fallback_max_cols=30,
        sparse_text_fallback_engine="tesseract",
        dense_grid_fallback=True,
        edge_word_fallback_min_tokens=1,
        image_preprocessing=(
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        ),
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 1400),
                ("min_region_height", 300),
                ("min_separator_coverage", 0.55),
            ),
        ),
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
        image_preprocessing=("projected_document_dewarp",),
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry",),
            selector="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 1400),
                ("min_region_height", 300),
                ("min_separator_coverage", 0.55),
            ),
        ),
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
