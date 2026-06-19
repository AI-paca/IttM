from collections.abc import Iterable

import numpy as np
from PIL import Image

from app.layout.contracts import (
    ComponentFeature,
    LayoutFeatures,
    SeparatorCandidate,
)

MAX_ANALYSIS_PIXELS = 8_000_000
MAX_ANALYSIS_DIMENSION = 12_000
MAX_COMPONENTS = 20_000


def _analysis_image(image: Image.Image) -> tuple[np.ndarray, float, float]:
    width, height = image.size
    pixel_scale = min(
        1.0,
        (MAX_ANALYSIS_PIXELS / max(1, width * height)) ** 0.5,
    )
    dimension_scale = min(
        1.0,
        MAX_ANALYSIS_DIMENSION / max(width, height),
    )
    scale = min(pixel_scale, dimension_scale)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    if (target_width, target_height) == image.size:
        gray = image.convert("L")
    else:
        resampling = getattr(Image, "Resampling", Image)
        gray = image.convert("L").resize(
            (target_width, target_height),
            resampling.BILINEAR,
        )
    return (
        np.asarray(gray),
        width / target_width,
        height / target_height,
    )


def _foreground_mask(gray: np.ndarray) -> np.ndarray:
    import cv2

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(
        blurred,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    block_size = max(15, min(51, (min(gray.shape[:2]) // 24) | 1))
    adaptive = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        11,
    )
    ink_ratio = float(np.mean(otsu > 0))
    if 0.0005 <= ink_ratio <= 0.25:
        return cv2.bitwise_or(otsu, adaptive) > 0
    return otsu > 0


def _groups(mask: np.ndarray) -> Iterable[tuple[int, int]]:
    indexes = np.flatnonzero(mask)
    if not indexes.size:
        return

    start = int(indexes[0])
    previous = start
    for raw_index in indexes[1:]:
        index = int(raw_index)
        if index == previous + 1:
            previous = index
            continue
        yield start, previous + 1
        start = index
        previous = index
    yield start, previous + 1


def _scaled_interval(start: int, end: int, scale: float, limit: int) -> tuple[int, int]:
    return (
        max(0, min(limit, int(round(start * scale)))),
        max(0, min(limit, int(round(end * scale)))),
    )


def _whitespace_candidates(
    binary: np.ndarray,
    *,
    x_scale: float,
    y_scale: float,
    source_width: int,
    source_height: int,
) -> list[SeparatorCandidate]:
    height, width = binary.shape
    candidates: list[SeparatorCandidate] = []

    row_density = np.mean(binary, axis=1)
    row_threshold = max(0.001, min(0.01, float(np.quantile(row_density, 0.2))))
    min_row_gap = max(2, int(round(height * 0.0007)))
    for start, end in _groups(row_density <= row_threshold):
        if end - start < min_row_gap:
            continue
        y1, y2 = _scaled_interval(start, end, y_scale, source_height)
        candidates.append(
            SeparatorCandidate(
                axis="y",
                start=y1,
                end=y2,
                span_start=0,
                span_end=source_width,
                kind="whitespace",
                strength=max(
                    0.0,
                    min(
                        1.0,
                        1.0 - float(np.mean(row_density[start:end])) / max(row_threshold, 1e-6),
                    ),
                ),
            )
        )

    window_heights = sorted({min(height, max(80, int(width * ratio))) for ratio in (0.5, 1.0, 1.75)})
    min_column_gap = max(4, int(round(width * 0.012)))
    edge_margin = max(2, int(round(width * 0.02)))
    for window_height in window_heights:
        step = max(1, window_height // 2)
        for top in range(0, height, step):
            bottom = min(height, top + window_height)
            if bottom - top < max(40, window_height // 3):
                break
            density = np.mean(binary[top:bottom], axis=0)
            threshold = max(
                0.002,
                min(0.035, float(np.quantile(density, 0.18)) * 1.5),
            )
            for start, end in _groups(density <= threshold):
                if end - start < min_column_gap:
                    continue
                if start < edge_margin or end > width - edge_margin:
                    continue
                x1, x2 = _scaled_interval(start, end, x_scale, source_width)
                y1, y2 = _scaled_interval(top, bottom, y_scale, source_height)
                candidates.append(
                    SeparatorCandidate(
                        axis="x",
                        start=x1,
                        end=x2,
                        span_start=y1,
                        span_end=y2,
                        kind="whitespace",
                        strength=max(
                            0.0,
                            min(
                                1.0,
                                1.0 - float(np.mean(density[start:end])) / max(threshold, 1e-6),
                            ),
                        ),
                    )
                )
            if bottom == height:
                break

    return candidates


def _ink_line_candidates(
    binary: np.ndarray,
    *,
    x_scale: float,
    y_scale: float,
    source_width: int,
    source_height: int,
) -> list[SeparatorCandidate]:
    import cv2

    height, width = binary.shape
    binary_u8 = binary.astype(np.uint8) * 255
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(18, width // 28), 1),
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (1, max(18, min(height, width * 2) // 28)),
    )
    horizontal = cv2.morphologyEx(
        binary_u8,
        cv2.MORPH_OPEN,
        horizontal_kernel,
    )
    vertical = cv2.morphologyEx(
        binary_u8,
        cv2.MORPH_OPEN,
        vertical_kernel,
    )

    candidates: list[SeparatorCandidate] = []
    for axis, mask, projection_axis, source_limit, scale, span_limit in (
        ("x", vertical, 0, source_width, x_scale, source_height),
        ("y", horizontal, 1, source_height, y_scale, source_width),
    ):
        projection = np.mean(mask > 0, axis=projection_axis)
        indexes = projection >= 0.18
        for start, end in _groups(indexes):
            line_start, line_end = _scaled_interval(
                start,
                end,
                scale,
                source_limit,
            )
            candidates.append(
                SeparatorCandidate(
                    axis=axis,
                    start=line_start,
                    end=line_end,
                    span_start=0,
                    span_end=span_limit,
                    kind="ink",
                    strength=min(1.0, float(np.max(projection[start:end]))),
                )
            )
    return candidates


def _component_features(
    binary: np.ndarray,
    *,
    x_scale: float,
    y_scale: float,
    source_width: int,
    source_height: int,
) -> tuple[ComponentFeature, ...]:
    import cv2

    kernel_width = max(2, int(round(binary.shape[1] * 0.003)))
    joined = cv2.morphologyEx(
        binary.astype(np.uint8) * 255,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 2)),
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        joined,
        connectivity=8,
    )
    components: list[ComponentFeature] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if area < 4 or width <= 0 or height <= 0:
            continue
        x1, x2 = _scaled_interval(x, x + width, x_scale, source_width)
        y1, y2 = _scaled_interval(y, y + height, y_scale, source_height)
        components.append(
            ComponentFeature(
                bbox=(x1, y1, x2, y2),
                area=max(1, int(round(area * x_scale * y_scale))),
                fill_ratio=min(1.0, area / max(1, width * height)),
            )
        )

    components.sort(key=lambda component: (-component.area, component.bbox))
    return tuple(components[:MAX_COMPONENTS])


class ProjectionGeometryExtractor:
    name = "projection_geometry"

    def extract(self, image: Image.Image) -> LayoutFeatures:
        try:
            import cv2  # noqa: F401
        except Exception:
            width, height = image.size
            return LayoutFeatures(
                width=width,
                height=height,
                foreground_ratio=0.0,
                scalars=(("extractor_available", False),),
            )

        width, height = image.size
        gray, x_scale, y_scale = _analysis_image(image)
        binary = _foreground_mask(gray)
        separators = [
            *_whitespace_candidates(
                binary,
                x_scale=x_scale,
                y_scale=y_scale,
                source_width=width,
                source_height=height,
            ),
            *_ink_line_candidates(
                binary,
                x_scale=x_scale,
                y_scale=y_scale,
                source_width=width,
                source_height=height,
            ),
        ]
        components = _component_features(
            binary,
            x_scale=x_scale,
            y_scale=y_scale,
            source_width=width,
            source_height=height,
        )
        return LayoutFeatures(
            width=width,
            height=height,
            foreground_ratio=float(np.mean(binary)),
            separators=tuple(separators),
            components=components,
            scalars=(
                ("analysis_height", int(gray.shape[0])),
                ("analysis_width", int(gray.shape[1])),
                ("aspect_ratio", height / max(1, width)),
                ("component_count", len(components)),
                ("extractor_available", True),
            ),
        )


LAYOUT_FEATURE_EXTRACTORS = {
    ProjectionGeometryExtractor.name: ProjectionGeometryExtractor,
}


def collect_layout_features(
    image: Image.Image,
    extractor_names: tuple[str, ...],
) -> LayoutFeatures:
    if not extractor_names:
        width, height = image.size
        return LayoutFeatures(
            width=width,
            height=height,
            foreground_ratio=0.0,
        )

    collected: list[LayoutFeatures] = []
    for name in extractor_names:
        extractor_type = LAYOUT_FEATURE_EXTRACTORS.get(name)
        if extractor_type is None:
            known = ", ".join(sorted(LAYOUT_FEATURE_EXTRACTORS))
            raise ValueError(f"Unknown layout feature extractor '{name}'. Known extractors: {known}")
        collected.append(extractor_type().extract(image))

    base = collected[0]
    scalars = dict(base.scalars)
    for features in collected[1:]:
        scalars.update(features.scalars)
    return LayoutFeatures(
        width=base.width,
        height=base.height,
        foreground_ratio=max(features.foreground_ratio for features in collected),
        separators=tuple(separator for features in collected for separator in features.separators),
        components=tuple(component for features in collected for component in features.components),
        scalars=tuple(sorted(scalars.items())),
    )
