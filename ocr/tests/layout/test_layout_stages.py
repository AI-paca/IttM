import pytest
from PIL import Image, ImageDraw

from app.layout.features import ProjectionGeometryExtractor
from app.layout.selectors import select_layout_pipeline
from app.layout.stages import execute_layout_decision


def _marker_page(columns: int, rows: int = 4):
    width = 1200
    header_height = 160
    gap = 30
    row_height = 260
    height = header_height + gap + rows * (row_height + gap)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    header_marker = (190, 30, 80)
    draw.rectangle(
        (20, 20, width - 20, 130),
        fill=(245, 245, 245),
        outline="black",
        width=4,
    )
    draw.rectangle((40, 40, 60, 60), fill=header_marker)
    draw.text((90, 55), "FULL WIDTH", fill="black")

    cell_width = (width - (columns + 1) * gap) // columns
    markers = []
    top = header_height + gap
    for row in range(rows):
        for column in range(columns):
            left = gap + column * (cell_width + gap)
            marker = (
                20 + row * 35,
                40 + column * 25,
                80 + row * 15 + column,
            )
            markers.append(marker)
            draw.rectangle(
                (left, top, left + cell_width, top + row_height),
                outline="black",
                width=3,
            )
            draw.rectangle(
                (left + 12, top + 12, left + 24, top + 24),
                fill=marker,
            )
            draw.text(
                (left + 35, top + 45),
                f"R{row}-C{column}",
                fill="black",
            )
        top += row_height + gap
    return image, header_marker, markers


def _colors(image: Image.Image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=image.width * image.height)}


@pytest.mark.parametrize("columns", [1, 2, 3, 6])
def test_spatial_stage_preserves_all_markers_for_arbitrary_column_counts(
    columns,
):
    pytest.importorskip("cv2")
    image, header_marker, markers = _marker_page(columns)
    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 700),
                ("min_region_height", 180),
            ),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
        region_colors = [_colors(region.image) for region in regions]
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert sum(header_marker in colors for colors in region_colors) == 1
    assert all(sum(marker in colors for colors in region_colors) == 1 for marker in markers)
    header_region = next(region for region, colors in zip(regions, region_colors) if header_marker in colors)
    assert header_region.bbox[0] == 0
    assert header_region.bbox[2] == image.width


def test_spatial_stage_keeps_single_large_word_in_one_region():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (3840, 2160), "white")
    draw = ImageDraw.Draw(image)
    draw.text((1500, 1000), "SALE", fill="black", stroke_width=8)
    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="uniform_spatial_v1",
            allowed_stages=("spatial_regions",),
            default_parameters=(
                ("max_region_height", 3000),
                ("min_region_height", 300),
            ),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0.35,
        )
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert len(regions) == 1


def test_table_partition_preserves_content_on_both_sides_of_grid():
    pytest.importorskip("cv2")
    image = Image.new("RGB", (1000, 500), "white")
    draw = ImageDraw.Draw(image)
    left_marker = (180, 20, 20)
    right_marker = (20, 20, 180)
    draw.rectangle((30, 210, 50, 230), fill=left_marker)
    draw.text((60, 200), "LEFT", fill="black")
    draw.rectangle((950, 210, 970, 230), fill=right_marker)
    draw.text((880, 200), "RIGHT", fill="black")
    for x in (250, 500, 750):
        draw.line((x, 80, x, 420), fill="black", width=4)
    for y in (80, 250, 420):
        draw.line((250, y, 750, y), fill="black", width=4)

    try:
        features = ProjectionGeometryExtractor().extract(image)
        decision = select_layout_pipeline(
            features,
            selector_name="fixed",
            allowed_stages=("table_regions",),
            default_parameters=(),
        )
        regions = execute_layout_decision(
            image,
            features,
            decision,
            min_confirmed_cell_ratio=0,
        )
        colors = [_colors(region.image) for region in regions]
    finally:
        for region in locals().get("regions", []):
            if region.image is not image:
                region.image.close()
        image.close()

    assert any(region.kind == "table" for region in regions)
    assert any(left_marker in value for value in colors)
    assert any(right_marker in value for value in colors)
