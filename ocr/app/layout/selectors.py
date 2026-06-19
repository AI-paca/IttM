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


LAYOUT_SELECTORS = {
    FixedLayoutSelector.name: FixedLayoutSelector,
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
