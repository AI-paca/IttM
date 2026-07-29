from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.sparse_pipeline.local_structure import (
    LocalStructureConfig,
    detect_local_structures,
)


def _canvas(size: tuple[int, int] = (80, 56)) -> Image.Image:
    return Image.new("RGB", size, (24, 24, 24))


def test_local_rectangle_keeps_observed_finite_endpoints() -> None:
    image = _canvas()
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 9, 43, 34), fill=(112, 112, 112))

    result = detect_local_structures(image)

    assert len(result.networks) == 1
    network = result.networks[0]
    assert len(network.horizontal_lines) == 2
    assert len(network.vertical_lines) == 2
    assert network.bbox == (12, 9, 45, 36)
    assert {line.bbox for line in network.horizontal_lines} == {
        (12, 9, 44, 10),
        (12, 35, 44, 36),
    }
    assert {line.bbox for line in network.vertical_lines} == {
        (12, 9, 13, 35),
        (44, 9, 45, 35),
    }
    assert all(
        0 < line.bbox[0] < line.bbox[2] < image.width and 0 < line.bbox[1] < line.bbox[3] < image.height
        for line in result.lines
    )


def test_disjoint_local_rectangles_are_not_joined_by_projected_axes() -> None:
    image = _canvas((100, 64))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 34, 27), fill=(90, 110, 130))
    draw.rectangle((59, 35, 89, 54), fill=(140, 80, 60))

    result = detect_local_structures(image)

    assert tuple(network.bbox for network in result.networks) == (
        (8, 8, 36, 29),
        (59, 35, 91, 56),
    )
    assert all(len(network.horizontal_lines) == 2 and len(network.vertical_lines) == 2 for network in result.networks)


def test_non_intersecting_finite_horizontal_and_vertical_lines_stay_separate() -> None:
    image = _canvas((64, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 11, 22, 12), fill=(180, 180, 180))
    draw.rectangle((39, 5, 40, 25), fill=(180, 180, 180))

    result = detect_local_structures(image)

    assert len(result.lines) == 2
    assert len(result.networks) == 2
    assert {line.axis for line in result.lines} == {"horizontal", "vertical"}


def test_physically_crossing_lines_form_one_local_network() -> None:
    image = _canvas((64, 48))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 22, 51, 23), fill=(180, 180, 180))
    draw.rectangle((29, 7, 30, 38), fill=(180, 180, 180))

    result = detect_local_structures(image)

    assert len(result.networks) == 1
    assert result.networks[0].horizontal_lines
    assert result.networks[0].vertical_lines


def test_isolated_line_is_retained_without_table_shape_gate() -> None:
    image = _canvas((64, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((9, 17, 46, 18), fill=(120, 120, 120))

    result = detect_local_structures(image)

    assert len(result.lines) == 1
    assert result.lines[0].axis == "horizontal"
    assert result.lines[0].bbox == (9, 17, 47, 20)
    assert len(result.networks) == 1
    assert result.networks[0].lines == result.lines
    assert not result.networks[0].vertical_lines


def test_gray_edges_do_not_require_chroma_or_page_wide_coverage() -> None:
    image = _canvas((120, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((47, 31, 70, 48), fill=(45, 45, 45))

    result = detect_local_structures(
        image,
        LocalStructureConfig(minimum_contrast=8, minimum_length=8),
    )

    assert len(result.networks) == 1
    assert result.networks[0].bbox == (47, 31, 72, 50)
    assert result.networks[0].bbox[2] - result.networks[0].bbox[0] < image.width // 2


def test_numpy_and_pil_inputs_match_without_mutating_source() -> None:
    image = _canvas((56, 44))
    draw = ImageDraw.Draw(image)
    draw.rectangle((7, 6, 31, 28), fill=(100, 60, 140))
    array = np.asarray(image).copy()
    before = array.copy()

    from_pil = detect_local_structures(image)
    from_numpy = detect_local_structures(array)

    assert from_numpy == from_pil
    assert np.array_equal(array, before)


def test_short_gaps_are_bridged_without_extending_the_observed_line() -> None:
    image = _canvas((64, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((7, 18, 22, 19), fill=(150, 150, 150))
    draw.rectangle((24, 18, 42, 19), fill=(150, 150, 150))

    result = detect_local_structures(
        image,
        LocalStructureConfig(maximum_gap=1),
    )

    horizontal = tuple(line for line in result.lines if line.axis == "horizontal")
    assert len(horizontal) == 1
    assert horizontal[0].bbox == (7, 18, 43, 21)


@pytest.mark.parametrize(
    "value,exception",
    (
        (np.zeros((4, 4), dtype=np.uint8), ValueError),
        (np.zeros((4, 4, 4), dtype=np.uint8), ValueError),
        (np.full((4, 4, 3), 300, dtype=np.int16), ValueError),
        ("not-rgb", TypeError),
    ),
)
def test_invalid_rgb_inputs_are_rejected(value: object, exception: type[Exception]) -> None:
    with pytest.raises(exception):
        detect_local_structures(value)  # type: ignore[arg-type]


def test_uniform_image_has_no_invented_lines_or_networks() -> None:
    result = detect_local_structures(_canvas())

    assert result.lines == ()
    assert result.networks == ()
