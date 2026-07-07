from dataclasses import dataclass

from app.layout.contracts import (
    FeatureValue,
    LayoutDecision,
    LayoutFeatures,
    LayoutStageSpec,
)


@dataclass(frozen=True)
class FixedLayoutSelector:
    name = "fixed"

    def select(
        self,
        features: LayoutFeatures,
        *,
        allowed_stages: tuple[str, ...],
        default_parameters: tuple[tuple[str, FeatureValue], ...],
    ) -> LayoutDecision:
        del features
        return LayoutDecision(
            label="fixed",
            stages=tuple(
                LayoutStageSpec(
                    name=stage,
                    parameters=default_parameters,
                )
                for stage in allowed_stages
            ),
            confidence=1.0,
        )


@dataclass(frozen=True)
class UniformSpatialSelector:
    """
    Temporary selector used until a trained layout selector is available.

    It deliberately makes no document-type prediction. Every non-empty image is
    passed to the same spatial layout stage with unbounded source dimensions.
    """

    name = "uniform_spatial_v1"

    def select(
        self,
        features: LayoutFeatures,
        *,
        allowed_stages: tuple[str, ...],
        default_parameters: tuple[tuple[str, FeatureValue], ...],
    ) -> LayoutDecision:
        if features.foreground_ratio <= 0:
            return LayoutDecision(
                label="empty",
                stages=(),
                confidence=1.0,
            )
        if "spatial_regions" not in allowed_stages:
            return LayoutDecision(
                label="unsegmented",
                stages=(),
                confidence=1.0,
            )

        parameters = {
            "min_source_width": 0,
            "max_source_width": "infinity",
            **dict(default_parameters),
        }
        return LayoutDecision(
            label="spatial",
            stages=(
                LayoutStageSpec(
                    name="spatial_regions",
                    parameters=tuple(sorted(parameters.items())),
                ),
            ),
            confidence=1.0,
        )


def _stage(
    name: str,
    parameters: dict[str, FeatureValue],
) -> LayoutStageSpec:
    return LayoutStageSpec(
        name=name,
        parameters=tuple(sorted(parameters.items())),
    )


def _scalar_number(
    features: LayoutFeatures,
    name: str,
    default: float,
) -> float:
    value = features.scalar(name, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return default
    return float(value)


def _separator_count(
    features: LayoutFeatures,
    *,
    axis: str,
    kind: str,
    min_strength: float,
) -> int:
    return sum(
        1
        for separator in features.separators
        if separator.axis == axis and separator.kind == kind and separator.strength >= min_strength
    )


@dataclass(frozen=True)
class TableFirstHeuristicSelector:
    """
    Conservative table-first selector.

    OCR pages are treated as possible grids first, but the selector keeps the
    expensive table stage for pages that expose real ink separators. Everything
    else falls through to spatial bands, which still partitions around detected
    tables before splitting whitespace blocks.
    """

    name = "table_first_heuristic_v1"

    def select(
        self,
        features: LayoutFeatures,
        *,
        allowed_stages: tuple[str, ...],
        default_parameters: tuple[tuple[str, FeatureValue], ...],
    ) -> LayoutDecision:
        parameters: dict[str, FeatureValue] = {
            "min_source_width": 0,
            "max_source_width": "infinity",
            "medium_page_segmentation": True,
            **dict(default_parameters),
        }
        aspect_ratio = _scalar_number(
            features,
            "aspect_ratio",
            features.height / max(1, features.width),
        )
        horizontal_ink = _separator_count(
            features,
            axis="y",
            kind="ink",
            min_strength=0.18,
        )
        vertical_ink = _separator_count(
            features,
            axis="x",
            kind="ink",
            min_strength=0.18,
        )
        horizontal_whitespace = _separator_count(
            features,
            axis="y",
            kind="whitespace",
            min_strength=0.5,
        )
        vertical_whitespace = _separator_count(
            features,
            axis="x",
            kind="whitespace",
            min_strength=0.5,
        )
        text_grid_score = min(
            1.0,
            (
                min(horizontal_ink / 8, 1.0) * 0.45
                + min(vertical_ink / 6, 1.0) * 0.45
                + min(vertical_whitespace / 6, 1.0) * 0.10
            ),
        )
        parameters.update(
            {
                "layout_class": "spatial_blocks",
                "text_grid_score": round(text_grid_score, 3),
                "text_row_tracks": horizontal_ink + horizontal_whitespace,
                "text_column_tracks": vertical_ink + vertical_whitespace,
                "multi_column_text_rows": vertical_whitespace,
                "aspect_ratio": round(aspect_ratio, 3),
            }
        )

        extractor_available = features.scalar("extractor_available", True)
        if extractor_available is False:
            parameters["layout_class"] = "analysis_unavailable"
            return LayoutDecision(
                label="analysis_unavailable",
                stages=(),
                confidence=0.0,
            )

        if features.foreground_ratio <= 0:
            parameters["layout_class"] = "empty"
            return LayoutDecision(
                label="empty",
                stages=(),
                confidence=1.0,
            )

        has_table_stage = "table_regions" in allowed_stages
        has_spatial_stage = "spatial_regions" in allowed_stages
        table_like = horizontal_ink >= 3 and vertical_ink >= 2
        dense_table_like = horizontal_ink >= 8 and (vertical_ink >= 1 or text_grid_score >= 0.45)

        if has_table_stage and (table_like or dense_table_like):
            parameters["layout_class"] = "table_like_grid"
            return LayoutDecision(
                label="table_like_grid",
                stages=(_stage("table_regions", parameters),),
                confidence=max(0.55, text_grid_score),
            )

        if has_spatial_stage:
            if aspect_ratio >= 4.0:
                parameters["layout_class"] = "long_vertical_blocks"
            elif vertical_whitespace >= 2:
                parameters["layout_class"] = "multi_column_blocks"
            return LayoutDecision(
                label=str(parameters["layout_class"]),
                stages=(_stage("spatial_regions", parameters),),
                confidence=0.65,
            )

        if has_table_stage:
            parameters["layout_class"] = "table_probe"
            return LayoutDecision(
                label="table_probe",
                stages=(_stage("table_regions", parameters),),
                confidence=0.35,
            )

        return LayoutDecision(
            label="unsegmented",
            stages=(),
            confidence=0.0,
        )


LAYOUT_SELECTORS = {
    FixedLayoutSelector.name: FixedLayoutSelector,
    TableFirstHeuristicSelector.name: TableFirstHeuristicSelector,
    UniformSpatialSelector.name: UniformSpatialSelector,
}


def select_layout_pipeline(
    features: LayoutFeatures,
    *,
    selector_name: str,
    allowed_stages: tuple[str, ...],
    default_parameters: tuple[tuple[str, FeatureValue], ...],
) -> LayoutDecision:
    selector_type = LAYOUT_SELECTORS.get(selector_name)
    if selector_type is None:
        known = ", ".join(sorted(LAYOUT_SELECTORS))
        raise ValueError(f"Unknown layout selector '{selector_name}'. Known selectors: {known}")
    return selector_type().select(
        features,
        allowed_stages=allowed_stages,
        default_parameters=default_parameters,
    )
