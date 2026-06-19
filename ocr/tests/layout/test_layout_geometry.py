import pytest
from PIL import Image, ImageDraw

from app.layout.features import ProjectionGeometryExtractor
from app.layout.selectors import select_layout_pipeline


def _column_page(
    columns: int,
    *,
    rows: int = 5,
    full_width_header: bool = True,
) -> Image.Image:
    width = 1200
    header_height = 150 if full_width_header else 0
    row_height = 260
    gap = 28
    height = header_height + rows * row_height + (rows + 1) * gap
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    if full_width_header:
        draw.rectangle(
            (30, 20, width - 30, header_height - 20),
            outline="black",
            width=4,
        )
        draw.text((70, 65), "FULL-WIDTH-HEADER", fill="black")

    usable_width = width - (columns + 1) * gap
    column_width = usable_width // columns
    top = header_height + gap
    for row in range(rows):
        for column in range(columns):
            left = gap + column * (column_width + gap)
            right = left + column_width
            bottom = top + row_height
            draw.rectangle((left, top, right, bottom), outline="black", width=3)
            draw.text(
                (left + 20, top + 30),
                f"R{row}-C{column}",
                fill="black",
            )
            draw.text(
                (left + 20, top + 90),
                f"{row + 1}{column + 1}99",
                fill="black",
            )
        top += row_height + gap
    return image


@pytest.mark.parametrize("columns", [1, 2, 3, 6])
def test_geometry_collects_virtual_separator_evidence_without_classifying_input(
    columns,
):
    pytest.importorskip("cv2")
    image = _column_page(columns)
    try:
        features = ProjectionGeometryExtractor().extract(image)
    finally:
        image.close()

    virtual_x = [
        separator
        for separator in features.separators
        if separator.axis == "x" and separator.kind == "whitespace" and separator.strength >= 0.5
    ]
    for divider in range(1, columns):
        expected = features.width * divider / columns
        assert any(
            separator.start <= expected <= separator.end
            or abs((separator.start + separator.end) / 2 - expected) <= features.width * 0.06
            for separator in virtual_x
        )


def test_single_large_word_does_not_create_fake_columns_or_table():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (3840, 2160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((500, 700, 3340, 1460), outline="black", width=20)
    draw.text((1500, 1030), "SALE", fill="black", stroke_width=5)
    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(),
        )
    finally:
        image.close()

    assert decision.label == "spatial"
    assert decision.stages[0].name == "spatial_regions"
    assert decision.stages[0].parameter("min_source_width") == 0
    assert decision.stages[0].parameter("max_source_width") == "infinity"


def test_selector_is_replaceable_without_recollecting_features():
    pytest.importorskip("cv2")
    image = _column_page(3)
    try:
        features = ProjectionGeometryExtractor().extract(image)
    finally:
        image.close()

    adaptive = select_layout_pipeline(
        features,
        selector_name="uniform_spatial_v1",
        allowed_stages=("spatial_regions",),
        default_parameters=(),
    )
    fixed = select_layout_pipeline(
        features,
        selector_name="fixed",
        allowed_stages=("table_regions",),
        default_parameters=(),
    )

    assert adaptive.label == "spatial"
    assert fixed.label == "fixed"
    assert fixed.stages[0].name == "table_regions"
