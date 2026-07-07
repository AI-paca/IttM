from dataclasses import replace

from PIL import Image

from app.chunking.vertical import LayoutRegion, mark_table_empty_slots
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
    regions = [
        replace(
            region,
            table=mark_table_empty_slots(region.image, region.table),
        )
        if region.kind == "table" and region.table is not None
        else region
        for region in regions
    ]
    return regions, decision
