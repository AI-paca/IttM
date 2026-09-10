"""Finite local line evidence extracted directly from RGB gradients.

This module deliberately stops before semantic layout classification.  It
records maximal horizontal and vertical gradient runs at their observed
endpoints, then groups only physically touching runs into local networks.
An isolated line is therefore still retained as a one-line network; callers
may later decide whether a network is a table, a card, an underline, or some
other object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from PIL import Image

LineAxis = Literal["horizontal", "vertical"]
Box = tuple[int, int, int, int]

__all__ = (
    "LocalLine",
    "LocalNetwork",
    "LocalStructureConfig",
    "LocalStructureResult",
    "detect_local_structures",
)


@dataclass(frozen=True)
class LocalStructureConfig:
    """Signal-scale limits for local RGB-gradient extraction.

    The limits describe raster evidence only.  They do not encode a table
    shape, a page-coverage fraction, or an expected object count.
    """

    minimum_contrast: int = 8
    minimum_length: int = 8
    maximum_gap: int = 1
    maximum_line_thickness: int = 4
    junction_tolerance: int = 2

    def __post_init__(self) -> None:
        values = (
            self.minimum_contrast,
            self.minimum_length,
            self.maximum_gap,
            self.maximum_line_thickness,
            self.junction_tolerance,
        )
        if any(type(value) is not int for value in values):
            raise ValueError("local structure limits must be integers")
        if not 1 <= self.minimum_contrast <= 255:
            raise ValueError("minimum_contrast must be between one and 255")
        if self.minimum_length < 1:
            raise ValueError("minimum_length must be positive")
        if self.maximum_gap < 0:
            raise ValueError("maximum_gap must be non-negative")
        if self.maximum_line_thickness < 1:
            raise ValueError("maximum_line_thickness must be positive")
        if self.junction_tolerance < 0:
            raise ValueError("junction_tolerance must be non-negative")


@dataclass(frozen=True)
class LocalLine:
    """One observed finite axial line band in aligned-image coordinates.

    A pixel gradient lies between two source pixels.  ``bbox`` gives that
    transition a positive one-pixel band on the greater-coordinate side; its
    long-axis endpoints remain the first and last observed gradient samples.
    """

    axis: LineAxis
    bbox: Box
    support_pixels: int
    strength: float

    def __post_init__(self) -> None:
        if self.axis not in ("horizontal", "vertical"):
            raise ValueError("local line axis must be horizontal or vertical")
        left, top, right, bottom = self.bbox
        if min(self.bbox) < 0 or left >= right or top >= bottom:
            raise ValueError("local line bbox must be a positive half-open box")
        if self.support_pixels < 1:
            raise ValueError("local line needs positive gradient support")
        if not 0.0 < self.strength <= 1.0:
            raise ValueError("local line strength must be in (0, 1]")

    @property
    def length(self) -> int:
        if self.axis == "horizontal":
            return self.bbox[2] - self.bbox[0]
        return self.bbox[3] - self.bbox[1]

    @property
    def thickness(self) -> int:
        if self.axis == "horizontal":
            return self.bbox[3] - self.bbox[1]
        return self.bbox[2] - self.bbox[0]


@dataclass(frozen=True)
class LocalNetwork:
    """A connected component of finite line evidence, without a type label."""

    bbox: Box
    lines: tuple[LocalLine, ...]

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a local network must contain at least one line")
        if self.bbox != _union_boxes(tuple(line.bbox for line in self.lines)):
            raise ValueError("local network bbox must be the exact union of its lines")

    @property
    def horizontal_lines(self) -> tuple[LocalLine, ...]:
        return tuple(line for line in self.lines if line.axis == "horizontal")

    @property
    def vertical_lines(self) -> tuple[LocalLine, ...]:
        return tuple(line for line in self.lines if line.axis == "vertical")


@dataclass(frozen=True)
class LocalStructureResult:
    """All finite lines and their exact connected-component partition."""

    lines: tuple[LocalLine, ...]
    networks: tuple[LocalNetwork, ...]

    def __post_init__(self) -> None:
        network_lines = tuple(line for network in self.networks for line in network.lines)
        if len(network_lines) != len(self.lines):
            raise ValueError("local networks must account for every line exactly once")
        if set(network_lines) != set(self.lines):
            raise ValueError("local networks must contain the detected lines")


@dataclass(frozen=True)
class _RunDraft:
    axis: LineAxis
    bbox: Box
    support_pixels: int
    gradient_sum: int


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parents = list(range(size))

    def find(self, value: int) -> int:
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self.parents[second_root] = first_root


def detect_local_structures(
    rgb: np.ndarray | Image.Image,
    config: LocalStructureConfig | None = None,
) -> LocalStructureResult:
    """Detect finite local horizontal/vertical RGB-gradient structures.

    The input is never modified.  Adjacent-pixel RGB differences provide the
    only evidence: no OCR text, fixture identity, table dimensions, global
    page coverage, or expected object count is consulted.
    """

    selected = config or LocalStructureConfig()
    values = _rgb_array(rgb)
    height, width, _ = values.shape
    if height < 2 or width < 2:
        return LocalStructureResult(lines=(), networks=())

    signed = values.astype(np.int16, copy=False)
    vertical_gradient = np.max(
        np.abs(signed[:, 1:] - signed[:, :-1]),
        axis=2,
    )
    horizontal_gradient = np.max(
        np.abs(signed[1:] - signed[:-1]),
        axis=2,
    )
    drafts = (
        *_axis_run_drafts(
            horizontal_gradient,
            axis="horizontal",
            threshold=selected.minimum_contrast,
            minimum_length=selected.minimum_length,
            maximum_gap=selected.maximum_gap,
        ),
        *_axis_run_drafts(
            vertical_gradient.T,
            axis="vertical",
            threshold=selected.minimum_contrast,
            minimum_length=selected.minimum_length,
            maximum_gap=selected.maximum_gap,
        ),
    )
    lines = _materialize_lines(drafts, selected)
    networks = _group_networks(lines, selected)
    return LocalStructureResult(lines=lines, networks=networks)


def _rgb_array(rgb: np.ndarray | Image.Image) -> np.ndarray:
    if isinstance(rgb, Image.Image):
        return np.asarray(rgb.convert("RGB"), dtype=np.uint8)
    if not isinstance(rgb, np.ndarray):
        raise TypeError("rgb must be a PIL image or a NumPy array")
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("rgb array must have shape (height, width, 3)")
    if rgb.shape[0] < 1 or rgb.shape[1] < 1:
        raise ValueError("rgb array dimensions must be positive")
    if not np.issubdtype(rgb.dtype, np.number) or np.issubdtype(rgb.dtype, np.complexfloating):
        raise TypeError("rgb array must have a real numeric dtype")
    if not bool(np.all(np.isfinite(rgb))):
        raise ValueError("rgb array must contain only finite values")
    if bool(np.any(rgb < 0)) or bool(np.any(rgb > 255)):
        raise ValueError("rgb array values must be between zero and 255")
    return rgb.astype(np.uint8, copy=False)


def _axis_run_drafts(
    gradient: np.ndarray,
    *,
    axis: LineAxis,
    threshold: int,
    minimum_length: int,
    maximum_gap: int,
) -> tuple[_RunDraft, ...]:
    drafts: list[_RunDraft] = []
    for fixed_index, values in enumerate(gradient):
        evidence = values >= threshold
        for start, stop in _true_runs(evidence, maximum_gap=maximum_gap):
            if stop - start < minimum_length:
                continue
            local_values = values[start:stop]
            local_evidence = evidence[start:stop]
            support_pixels = int(local_evidence.sum())
            if support_pixels == 0:
                continue
            coordinate = fixed_index + 1
            if axis == "horizontal":
                bbox = (start, coordinate, stop, coordinate + 1)
            else:
                bbox = (coordinate, start, coordinate + 1, stop)
            drafts.append(
                _RunDraft(
                    axis=axis,
                    bbox=bbox,
                    support_pixels=support_pixels,
                    gradient_sum=int(local_values[local_evidence].sum()),
                )
            )
    return tuple(drafts)


def _true_runs(values: np.ndarray, *, maximum_gap: int) -> tuple[tuple[int, int], ...]:
    indexes = np.flatnonzero(values)
    if not indexes.size:
        return ()
    runs: list[tuple[int, int]] = []
    start = int(indexes[0])
    previous = start
    for raw_index in indexes[1:]:
        index = int(raw_index)
        if index - previous - 1 <= maximum_gap:
            previous = index
            continue
        runs.append((start, previous + 1))
        start = index
        previous = index
    runs.append((start, previous + 1))
    return tuple(runs)


def _materialize_lines(
    drafts: tuple[_RunDraft, ...],
    config: LocalStructureConfig,
) -> tuple[LocalLine, ...]:
    disjoint = _DisjointSet(len(drafts))
    for axis in ("horizontal", "vertical"):
        indexes = sorted(
            (index for index, draft in enumerate(drafts) if draft.axis == axis),
            key=lambda index: _across_interval(drafts[index].bbox, axis),
        )
        active: list[int] = []
        for index in indexes:
            current = drafts[index]
            current_start = _across_interval(current.bbox, axis)[0]
            active = [
                other_index
                for other_index in active
                if _across_interval(drafts[other_index].bbox, axis)[1] + config.maximum_line_thickness >= current_start
            ]
            for other_index in active:
                other = drafts[other_index]
                if _same_line_band(other.bbox, current.bbox, axis, config):
                    disjoint.union(other_index, index)
            active.append(index)

    groups: dict[int, list[_RunDraft]] = {}
    for index, draft in enumerate(drafts):
        groups.setdefault(disjoint.find(index), []).append(draft)

    lines = tuple(
        LocalLine(
            axis=members[0].axis,
            bbox=_union_boxes(tuple(member.bbox for member in members)),
            support_pixels=sum(member.support_pixels for member in members),
            strength=min(
                1.0,
                sum(member.gradient_sum for member in members)
                / max(1, 255 * sum(member.support_pixels for member in members)),
            ),
        )
        for members in groups.values()
    )
    return tuple(sorted(lines, key=_line_order_key))


def _same_line_band(
    first: Box,
    second: Box,
    axis: LineAxis,
    config: LocalStructureConfig,
) -> bool:
    if axis == "horizontal":
        first_along = (first[0], first[2])
        second_along = (second[0], second[2])
        first_across = (first[1], first[3])
        second_across = (second[1], second[3])
    else:
        first_along = (first[1], first[3])
        second_along = (second[1], second[3])
        first_across = (first[0], first[2])
        second_across = (second[0], second[2])
    endpoint_tolerance = config.maximum_gap + config.maximum_line_thickness
    return (
        _interval_gap(first_across, second_across) <= config.maximum_line_thickness
        and abs(first_along[0] - second_along[0]) <= endpoint_tolerance
        and abs(first_along[1] - second_along[1]) <= endpoint_tolerance
    )


def _group_networks(
    lines: tuple[LocalLine, ...],
    config: LocalStructureConfig,
) -> tuple[LocalNetwork, ...]:
    disjoint = _DisjointSet(len(lines))
    cell_size = max(
        8,
        config.minimum_length,
        2 * (config.maximum_line_thickness + config.junction_tolerance) + 1,
    )
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, line in enumerate(lines):
        keys = _box_grid_keys(
            line.bbox,
            cell_size=cell_size,
            padding=config.junction_tolerance,
        )
        candidates = {other_index for key in keys for other_index in buckets.get(key, ())}
        for other_index in candidates:
            if _lines_touch(
                lines[other_index],
                line,
                config.junction_tolerance,
            ):
                disjoint.union(other_index, index)
        for key in keys:
            buckets.setdefault(key, []).append(index)

    groups: dict[int, list[LocalLine]] = {}
    for index, line in enumerate(lines):
        groups.setdefault(disjoint.find(index), []).append(line)
    networks = tuple(
        LocalNetwork(
            bbox=_union_boxes(tuple(line.bbox for line in members)),
            lines=tuple(sorted(members, key=_line_order_key)),
        )
        for members in groups.values()
    )
    return tuple(sorted(networks, key=lambda network: (network.bbox[1], network.bbox[0], network.bbox)))


def _lines_touch(first: LocalLine, second: LocalLine, tolerance: int) -> bool:
    if first.axis != second.axis:
        horizontal = first if first.axis == "horizontal" else second
        vertical = second if first.axis == "horizontal" else first
        return (
            _interval_gap(
                (horizontal.bbox[0], horizontal.bbox[2]),
                (vertical.bbox[0], vertical.bbox[2]),
            )
            <= tolerance
            and _interval_gap(
                (horizontal.bbox[1], horizontal.bbox[3]),
                (vertical.bbox[1], vertical.bbox[3]),
            )
            <= tolerance
        )
    if first.axis == "horizontal":
        along = _interval_gap((first.bbox[0], first.bbox[2]), (second.bbox[0], second.bbox[2]))
        across = _interval_gap((first.bbox[1], first.bbox[3]), (second.bbox[1], second.bbox[3]))
    else:
        along = _interval_gap((first.bbox[1], first.bbox[3]), (second.bbox[1], second.bbox[3]))
        across = _interval_gap((first.bbox[0], first.bbox[2]), (second.bbox[0], second.bbox[2]))
    return along <= tolerance and across <= tolerance


def _interval_gap(first: tuple[int, int], second: tuple[int, int]) -> int:
    if first[1] < second[0]:
        return second[0] - first[1]
    if second[1] < first[0]:
        return first[0] - second[1]
    return 0


def _across_interval(box: Box, axis: LineAxis) -> tuple[int, int]:
    if axis == "horizontal":
        return (box[1], box[3])
    return (box[0], box[2])


def _box_grid_keys(
    box: Box,
    *,
    cell_size: int,
    padding: int,
) -> tuple[tuple[int, int], ...]:
    left = max(0, box[0] - padding)
    top = max(0, box[1] - padding)
    # Include the half-open box endpoint in the spatial key range because
    # junctions are allowed to meet exactly at that coordinate.
    right = box[2] + padding + 1
    bottom = box[3] + padding + 1
    return tuple(
        (row, column)
        for row in range(top // cell_size, (bottom - 1) // cell_size + 1)
        for column in range(left // cell_size, (right - 1) // cell_size + 1)
    )


def _union_boxes(boxes: tuple[Box, ...]) -> Box:
    if not boxes:
        raise ValueError("cannot unite an empty box sequence")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _line_order_key(line: LocalLine) -> tuple[int, int, int, int, int]:
    return (
        0 if line.axis == "horizontal" else 1,
        line.bbox[1],
        line.bbox[0],
        line.bbox[3],
        line.bbox[2],
    )
