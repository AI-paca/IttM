#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.layout.contracts import LayoutDecision, LayoutFeatures, LayoutStageSpec
from app.layout.stages import execute_layout_decision
from app.preprocessing import OcrPreprocessingPipeline

Box = tuple[int, int, int, int]

PALETTE = (
    (17, 211, 190),
    (232, 28, 204),
    (245, 138, 7),
    (45, 32, 226),
    (234, 217, 17),
    (139, 63, 212),
    (14, 165, 233),
    (239, 68, 68),
)
GRID_COLOR = (65, 205, 63, 235)
TRACK_COLOR = (250, 250, 250, 225)
GROUP_COLOR = (255, 232, 62, 245)
MERGE_UP_CODES = frozenset({3, 8})
MERGE_LEFT_CODES = frozenset({5, 8})
DEFAULT_PREPROCESSING = (
    "projector_slide_dewarp",
    "mobile_screen_upscale",
    "small_text_upscale",
    "projected_document_dewarp",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the recursive OCR sparse grid as a debug color overlay.",
    )
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/tmp/ittm-recursive-grid"),
    )
    parser.add_argument("--occupied-alpha", type=int, default=104)
    parser.add_argument("--empty-alpha", type=int, default=32)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Skip preprocessing and render the layout stage against the source pixels.",
    )
    return parser.parse_args()


def _stage() -> LayoutStageSpec:
    return LayoutStageSpec(
        name="recursive_grid",
        parameters=(
            ("max_region_height", 1400),
            ("min_region_height", 120),
            ("min_region_width", 80),
            ("min_separator_gap", 8),
            ("max_depth", 32),
        ),
    )


def _regions(image: Image.Image):
    features = LayoutFeatures(
        width=image.width,
        height=image.height,
        foreground_ratio=0.0,
    )
    decision = LayoutDecision(
        label="debug-recursive-grid",
        stages=(_stage(),),
        confidence=1.0,
    )
    return execute_layout_decision(
        image,
        features,
        decision,
        min_confirmed_cell_ratio=0.0,
    )


def _ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, int))


def _box(value: object, fallback: Box) -> Box:
    if (
        isinstance(value, (tuple, list))
        and len(value) == 4
        and all(isinstance(item, int) for item in value)
    ):
        return tuple(value)  # type: ignore[return-value]
    return fallback


def _merge_tracks(values: Iterable[int], tolerance: int) -> tuple[int, ...]:
    tracks = sorted(set(values))
    if not tracks:
        return ()
    groups: list[list[int]] = [[tracks[0]]]
    for track in tracks[1:]:
        if track - groups[-1][-1] <= tolerance:
            groups[-1].append(track)
        else:
            groups.append([track])
    return tuple(round(sum(group) / len(group)) for group in groups)


def _global_tracks(regions, width: int) -> tuple[int, ...]:
    values = [0, width]
    for region in regions:
        if region.table is not None:
            offset = region.bbox[0]
            values.extend(offset + position for position in region.table.x_lines)
        metadata = region.metadata or {}
        values.extend(_ints(metadata.get("grid_x_left_tracks")))
        values.extend(_ints(metadata.get("soft_left_tracks")))
    return _merge_tracks(values, tolerance=max(4, width // 250))


def _column_index(track: int, global_tracks: tuple[int, ...]) -> int:
    if not global_tracks:
        return 0
    return min(
        range(len(global_tracks)),
        key=lambda index: abs(global_tracks[index] - track),
    )


def _fill(
    overlay: Image.Image,
    bbox: Box,
    column: int,
    alpha: int,
) -> None:
    left, top, right, bottom = bbox
    if right <= left or bottom <= top:
        return
    color = PALETTE[column % len(PALETTE)]
    ImageDraw.Draw(overlay).rectangle(
        (left, top, right - 1, bottom - 1),
        fill=(*color, alpha),
    )


def _draw_fake_row(
    overlay: Image.Image,
    region,
    global_tracks: tuple[int, ...],
    *,
    occupied_alpha: int,
    empty_alpha: int,
) -> None:
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = region.bbox
    metadata = region.metadata or {}
    content_left, _, content_right, _ = _box(
        metadata.get("content_bbox"),
        region.bbox,
    )
    local_tracks = tuple(
        sorted(
            {
                max(left, min(right, track))
                for track in _ints(metadata.get("soft_left_tracks"))
            }
        )
    )
    if not local_tracks:
        local_tracks = (content_left,)

    row_tracks = tuple(
        track for track in global_tracks if left <= track <= right
    )
    for start, end in zip(row_tracks, row_tracks[1:]):
        _fill(
            overlay,
            (start, top, end, bottom),
            _column_index(start, global_tracks),
            empty_alpha,
        )

    starts = tuple(track for track in local_tracks if track < content_right)
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else content_right
        _fill(
            overlay,
            (start, top, max(start + 1, end), bottom),
            _column_index(start, global_tracks),
            occupied_alpha,
        )
        draw.line((start, top, start, bottom), fill=GRID_COLOR, width=3)

    draw.rectangle(
        (left, top, max(left, right - 1), max(top, bottom - 1)),
        outline=GRID_COLOR,
        width=3,
    )


def _draw_table(
    overlay: Image.Image,
    region,
    *,
    occupied_alpha: int,
) -> None:
    if region.table is None:
        return
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    offset_x, offset_y = region.bbox[:2]
    row_boxes: dict[int, list[int]] = {}
    for cell in region.table.cells:
        left, top, right, bottom = cell.bbox
        bbox = (
            left + offset_x,
            top + offset_y,
            right + offset_x,
            bottom + offset_y,
        )
        _fill(overlay, bbox, cell.col, occupied_alpha)
        draw.rectangle(
            (bbox[0], bbox[1], max(bbox[0], bbox[2] - 1), max(bbox[1], bbox[3] - 1)),
            outline=GRID_COLOR,
            width=3,
        )
        row_box = row_boxes.setdefault(
            cell.row,
            [bbox[0], bbox[1], bbox[2], bbox[3]],
        )
        row_box[0] = min(row_box[0], bbox[0])
        row_box[1] = min(row_box[1], bbox[1])
        row_box[2] = max(row_box[2], bbox[2])
        row_box[3] = max(row_box[3], bbox[3])

    for row, (left, top, right, bottom) in row_boxes.items():
        draw.rectangle(
            (left, top, max(left, right - 1), max(top, bottom - 1)),
            outline=GROUP_COLOR,
            width=4,
        )
        draw.text(
            (left + 5, top + 4),
            f"r{row}",
            fill=(0, 0, 0, 255),
            stroke_width=2,
            stroke_fill=(255, 255, 255, 235),
            font=font,
        )


def _draw_legend(
    overlay: Image.Image,
    global_tracks: tuple[int, ...],
) -> None:
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    x = 8
    y = 8
    for index, track in enumerate(global_tracks[:-1]):
        label = f"c{index} x={track}"
        width = draw.textlength(label, font=font)
        color = PALETTE[index % len(PALETTE)]
        draw.rectangle(
            (x, y, x + width + 12, y + 18),
            fill=(*color, 220),
            outline=(255, 255, 255, 235),
        )
        draw.text((x + 6, y + 4), label, fill=(0, 0, 0, 255), font=font)
        x += int(width) + 18
        if x > overlay.width - 120:
            x = 8
            y += 22


def _draw_merge_groups(
    overlay: Image.Image,
    regions,
) -> None:
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    entries = []
    occupied = set()
    codes = set()
    for region in regions:
        metadata = region.metadata or {}
        row = metadata.get("grid_row")
        column = metadata.get("grid_col")
        sparse_codes = metadata.get("sparse_codes")
        if not isinstance(row, int) or not isinstance(column, int):
            continue
        anchor = (row, column)
        occupied.add(anchor)
        region_codes = (
            tuple(sparse_codes)
            if isinstance(sparse_codes, tuple)
            else ()
        )
        for code in region_codes:
            if (
                isinstance(code, tuple)
                and len(code) == 3
                and all(isinstance(value, int) for value in code)
            ):
                codes.add(code)
                occupied.add((code[0], code[1]))
                if code[2] in MERGE_UP_CODES:
                    occupied.add((code[0] - 1, code[1]))
        entries.append((region, anchor, region_codes))
    if not entries:
        return

    parent = {cell: cell for cell in occupied}

    def find(cell):
        while parent[cell] != cell:
            parent[cell] = parent[parent[cell]]
            cell = parent[cell]
        return cell

    def union(first, second):
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    by_row: dict[int, list[tuple[int, int]]] = {}
    for cell in occupied:
        by_row.setdefault(cell[0], []).append(cell)
    for _, anchor, region_codes in entries:
        row_points = {
            anchor,
            *(
                (row, column)
                for row, column, _ in region_codes
                if row == anchor[0]
            ),
        }
        for point in row_points:
            union(anchor, point)
    for row, column, code in codes:
        cell = (row, column)
        if code in MERGE_UP_CODES:
            union(cell, (row - 1, column))
        if code in MERGE_LEFT_CODES:
            left = [
                candidate
                for candidate in by_row.get(row, [])
                if candidate[1] < column
            ]
            if left:
                union(cell, max(left, key=lambda candidate: candidate[1]))

    groups: dict[tuple[int, int], list[int]] = {}
    for region, anchor, _ in entries:
        root = find(anchor)
        left, top, right, bottom = region.bbox
        bbox = groups.setdefault(root, [left, top, right, bottom])
        bbox[0] = min(bbox[0], left)
        bbox[1] = min(bbox[1], top)
        bbox[2] = max(bbox[2], right)
        bbox[3] = max(bbox[3], bottom)

    for group, (left, top, right, bottom) in enumerate(
        sorted(groups.values(), key=lambda bbox: (bbox[1], bbox[0]))
    ):
        draw.rectangle(
            (left, top, max(left, right - 1), max(top, bottom - 1)),
            outline=GROUP_COLOR,
            width=5,
        )
        draw.text(
            (left + 6, top + 5),
            f"g{group}",
            fill=(0, 0, 0, 255),
            stroke_width=2,
            stroke_fill=(255, 255, 255, 235),
            font=font,
        )

    tracks = next(
        (
            _ints((region.metadata or {}).get("grid_x_left_tracks"))
            for region, _, _ in entries
            if _ints(
                (region.metadata or {}).get("grid_x_left_tracks")
            )
        ),
        (),
    )
    region_by_row = {
        anchor[0]: region
        for region, anchor, _ in entries
    }
    for row, column, code in sorted(codes):
        region = region_by_row.get(row)
        if region is None:
            continue
        x = tracks[column] if column < len(tracks) else region.bbox[0]
        y = region.bbox[1] + 5
        draw.text(
            (x + 5, y),
            str(code),
            fill=(0, 0, 0, 255),
            stroke_width=2,
            stroke_fill=(255, 255, 255, 235),
            font=font,
        )


def _render(
    source: Path,
    output_dir: Path,
    *,
    occupied_alpha: int,
    empty_alpha: int,
    raw: bool,
) -> Path:
    with Image.open(source) as opened:
        original = opened.convert("RGB")
    pipeline = OcrPreprocessingPipeline.from_step_names(
        () if raw else DEFAULT_PREPROCESSING,
    )
    image = pipeline.apply(original)
    if image is not original:
        original.close()
    regions = _regions(image)
    global_tracks = _global_tracks(regions, image.width)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))

    for region in regions:
        if region.table is not None:
            _draw_table(
                overlay,
                region,
                occupied_alpha=occupied_alpha,
            )
        else:
            _draw_fake_row(
                overlay,
                region,
                global_tracks,
                occupied_alpha=occupied_alpha,
                empty_alpha=empty_alpha,
            )
    _draw_merge_groups(overlay, regions)
    _draw_legend(overlay, global_tracks)

    output_dir.mkdir(parents=True, exist_ok=True)
    aligned = output_dir / f"{source.name}.aligned.png"
    image.save(aligned)
    output = output_dir / f"{source.name}.recursive-grid.png"
    Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB").save(output)

    for region in regions:
        if region.image is not image:
            region.image.close()
    image.close()
    overlay.close()
    return output


def main() -> int:
    args = _parse_args()
    occupied_alpha = max(0, min(255, args.occupied_alpha))
    empty_alpha = max(0, min(255, args.empty_alpha))
    for source in args.images:
        output = _render(
            source,
            args.output_dir,
            occupied_alpha=occupied_alpha,
            empty_alpha=empty_alpha,
            raw=args.raw,
        )
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
