from dataclasses import dataclass


@dataclass(frozen=True)
class OcrPipelineProfile:
    name: str
    image_preprocessing: tuple[str, ...] = ()
    layout_analysis: tuple[str, ...] = ()


OCR_PIPELINE_PROFILES: dict[str, OcrPipelineProfile] = {
    "backend_auto_standard": OcrPipelineProfile(
        name="backend_auto_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
    ),
    "backend_tesseract_standard": OcrPipelineProfile(
        name="backend_tesseract_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
    ),
    "backend_easyocr_standard": OcrPipelineProfile(
        name="backend_easyocr_standard",
        image_preprocessing=("projected_document_dewarp",),
        layout_analysis=("table_layout",),
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
