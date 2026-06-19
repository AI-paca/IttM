from app.layout.contracts import (
    ComponentFeature,
    LayoutDecision,
    LayoutFeatures,
    LayoutStageSpec,
    SeparatorCandidate,
)
from app.pipeline_config import LayoutPipelineConfig, OcrPipelineProfile
from app.pipeline_config import OCR_PIPELINE_PROFILES


def test_layout_contract_keeps_observations_separate_from_selector_decision():
    features = LayoutFeatures(
        width=1200,
        height=9000,
        foreground_ratio=0.12,
        separators=(
            SeparatorCandidate(
                axis="x",
                start=390,
                end=410,
                span_start=0,
                span_end=9000,
                kind="whitespace",
                strength=0.98,
            ),
            SeparatorCandidate(
                axis="x",
                start=790,
                end=810,
                span_start=0,
                span_end=9000,
                kind="ink",
                strength=0.91,
            ),
        ),
        components=(
            ComponentFeature(
                bbox=(20, 30, 180, 120),
                area=14_400,
                fill_ratio=0.4,
            ),
        ),
        scalars=(("aspect_ratio", 7.5),),
    )
    decision = LayoutDecision(
        label="repeated_regions",
        stages=(
            LayoutStageSpec(
                name="xy_cut_regions",
                parameters=(("max_columns", 6),),
            ),
        ),
        confidence=0.88,
    )

    assert features.scalar("aspect_ratio") == 7.5
    assert {separator.kind for separator in features.separators} == {
        "ink",
        "whitespace",
    }
    assert decision.stages[0].parameter("max_columns") == 6


def test_profile_limits_selector_to_explicitly_allowed_layout_stages():
    profile = OcrPipelineProfile(
        name="adaptive",
        layout=LayoutPipelineConfig(
            feature_extractors=("projection_geometry", "connected_components"),
            selector="heuristic_v1",
            allowed_stages=("table_regions", "xy_cut_regions"),
            default_parameters=(("max_columns", 6),),
        ),
    )

    assert profile.layout.feature_extractors == (
        "projection_geometry",
        "connected_components",
    )
    assert profile.layout.selector == "heuristic_v1"
    assert profile.layout_analysis == ("table_regions", "xy_cut_regions")


def test_standard_backend_profiles_enable_uniform_spatial_layout():
    for name in (
        "backend_auto_standard",
        "backend_tesseract_standard",
        "backend_easyocr_standard",
    ):
        assert OCR_PIPELINE_PROFILES[name].image_preprocessing == (
            "projector_slide_dewarp",
            "mobile_screen_upscale",
            "small_text_upscale",
            "projected_document_dewarp",
        )
        layout = OCR_PIPELINE_PROFILES[name].layout
        assert layout.feature_extractors == ("projection_geometry",)
        assert layout.selector == "uniform_spatial_v1"
        assert layout.allowed_stages == ("spatial_regions",)


def test_easyocr_table_profile_keeps_table_only_diagnostic_path():
    layout = OCR_PIPELINE_PROFILES["backend_easyocr_table"].layout
    assert layout.feature_extractors == ()
    assert layout.selector == "fixed"
    assert layout.allowed_stages == ("table_regions",)


def test_easyocr_spatial_profile_is_explicitly_experimental():
    layout = OCR_PIPELINE_PROFILES["backend_easyocr_spatial"].layout
    assert layout.feature_extractors == ("projection_geometry",)
    assert layout.selector == "uniform_spatial_v1"
    assert layout.allowed_stages == ("spatial_regions",)

    easy_parameters = dict(layout.default_parameters)
    assert easy_parameters["direct_region_ocr"] is True
    assert easy_parameters["max_region_height"] == 2800
