from dataclasses import dataclass


@dataclass(frozen=True)
class OcrPipelineProfile:
    name: str
    image_preprocessing: tuple[str, ...] = ()
    layout_analysis: tuple[str, ...] = ()
    grid_min_confirmed_cell_ratio: float = 0.0
    table_min_word_cell_coverage: float = 0.35
    wide_table_min_word_cell_coverage: float = 0.02
    table_min_cell_coverage: float = 0.5
    max_table_cell_ocr_calls: int = 16


OCR_PIPELINE_PROFILES: dict[str, OcrPipelineProfile] = {
    "backend_auto_standard": OcrPipelineProfile(
        name="backend_auto_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_tesseract_standard": OcrPipelineProfile(
        name="backend_tesseract_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_easyocr_standard": OcrPipelineProfile(
        name="backend_easyocr_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
        grid_min_confirmed_cell_ratio=0.35,
    ),
    "backend_plain_text": OcrPipelineProfile(
        name="backend_plain_text",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=(),
    ),
    "backend_raw": OcrPipelineProfile(
        name="backend_raw",
        image_preprocessing=(),
        layout_analysis=(),
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
