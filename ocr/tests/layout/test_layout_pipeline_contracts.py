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


def test_standard_backend_profiles_keep_recursive_grid_layout_baseline():
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
        assert layout.feature_extractors == ()
        assert layout.selector == "fixed"
        assert layout.allowed_stages == ("recursive_grid",)
        assert dict(layout.default_parameters)["min_separator_gap"] == 8


def test_table_first_profiles_are_explicitly_experimental():
    for name in (
        "backend_auto_table_first",
        "backend_tesseract_table_first",
        "backend_easyocr_table_first",
    ):
        layout = OCR_PIPELINE_PROFILES[name].layout
        assert layout.feature_extractors == ("projection_geometry",)
        assert layout.selector == "table_first_heuristic_v1"
        assert layout.allowed_stages == ("table_regions", "spatial_regions")


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


def test_table_first_selector_prefers_line_grid_when_available():
    features = LayoutFeatures(
        width=1200,
        height=900,
        foreground_ratio=0.12,
        separators=(
            *(SeparatorCandidate("y", index * 100, index * 100 + 2, 0, 1200, "ink", 0.9) for index in range(1, 5)),
            *(SeparatorCandidate("x", index * 200, index * 200 + 2, 0, 900, "ink", 0.9) for index in range(1, 4)),
        ),
    )

    from app.layout.selectors import select_layout_pipeline

    decision = select_layout_pipeline(
        features,
        selector_name="table_first_heuristic_v1",
        allowed_stages=("table_regions", "spatial_regions"),
        default_parameters=(),
    )

    assert decision.label == "table_like_grid"
    assert decision.stages[0].name == "table_regions"
    assert decision.stages[0].parameter("layout_class") == "table_like_grid"


def test_table_first_selector_keeps_vertical_pages_as_spatial_blocks():
    features = LayoutFeatures(
        width=800,
        height=4200,
        foreground_ratio=0.08,
        scalars=(("aspect_ratio", 5.25), ("extractor_available", True)),
    )

    from app.layout.selectors import select_layout_pipeline

    decision = select_layout_pipeline(
        features,
        selector_name="table_first_heuristic_v1",
        allowed_stages=("table_regions", "spatial_regions"),
        default_parameters=(),
    )

    assert decision.label == "long_vertical_blocks"
    assert decision.stages[0].name == "spatial_regions"
