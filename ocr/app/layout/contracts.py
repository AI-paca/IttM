from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

Box: TypeAlias = tuple[int, int, int, int]
FeatureValue: TypeAlias = bool | float | int | str
SeparatorAxis: TypeAlias = Literal["x", "y"]
SeparatorKind: TypeAlias = Literal["ink", "whitespace"]


@dataclass(frozen=True)
class SeparatorCandidate:
    axis: SeparatorAxis
    start: int
    end: int
    span_start: int
    span_end: int
    kind: SeparatorKind
    strength: float


@dataclass(frozen=True)
class ComponentFeature:
    bbox: Box
    area: int
    fill_ratio: float


@dataclass(frozen=True)
class LayoutFeatures:
    width: int
    height: int
    foreground_ratio: float
    separators: tuple[SeparatorCandidate, ...] = ()
    components: tuple[ComponentFeature, ...] = ()
    scalars: tuple[tuple[str, FeatureValue], ...] = ()

    def scalar(self, name: str, default: FeatureValue | None = None) -> FeatureValue | None:
        return dict(self.scalars).get(name, default)

    def with_scalars(self, **values: FeatureValue) -> "LayoutFeatures":
        merged = dict(self.scalars)
        merged.update(values)
        return LayoutFeatures(
            width=self.width,
            height=self.height,
            foreground_ratio=self.foreground_ratio,
            separators=self.separators,
            components=self.components,
            scalars=tuple(sorted(merged.items())),
        )


@dataclass(frozen=True)
class LayoutStageSpec:
    name: str
    parameters: tuple[tuple[str, FeatureValue], ...] = ()

    def parameter(self, name: str, default: FeatureValue | None = None) -> FeatureValue | None:
        return dict(self.parameters).get(name, default)


@dataclass(frozen=True)
class LayoutDecision:
    label: str
    stages: tuple[LayoutStageSpec, ...]
    confidence: float


class LayoutSelector(Protocol):
    def select(
        self,
        features: LayoutFeatures,
        *,
        allowed_stages: tuple[str, ...],
        default_parameters: tuple[tuple[str, FeatureValue], ...],
    ) -> LayoutDecision:
        pass


class LayoutFeatureExtractor(Protocol):
    name: str

    def extract(self, image) -> LayoutFeatures:
        pass
