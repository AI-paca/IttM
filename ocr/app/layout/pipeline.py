from PIL import Image

from app.chunking.vertical import LayoutRegion
from app.layout.contracts import LayoutDecision
from app.layout.features import collect_layout_features
from app.layout.selectors import select_layout_pipeline
from app.layout.stages import execute_layout_decision
from app.pipeline_config import LayoutPipelineConfig


def analyze_layout(
    image: Image.Image,
    config: LayoutPipelineConfig,
    *,
    min_confirmed_cell_ratio: float,
) -> tuple[list[LayoutRegion], LayoutDecision]:
    features = collect_layout_features(
        image,
        config.feature_extractors,
    )
    decision = select_layout_pipeline(
        features,
        selector_name=config.selector,
        allowed_stages=config.allowed_stages,
        default_parameters=config.default_parameters,
    )
    regions = execute_layout_decision(
        image,
        features,
        decision,
        min_confirmed_cell_ratio=min_confirmed_cell_ratio,
    )
    return regions, decision
