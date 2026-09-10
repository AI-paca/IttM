from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, replace
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.sparse_pipeline.adaptive_language_ocr import (
    AdaptivePersistentOcrSession,
    CompactionRasterKind,
    GrammarAssessment,
    _Tile,
    _bounded_locality_family_slots,
    _pack_local_tiles,
    _pack_tiles,
    _render_canonical_locality_raster,
    build_deferred_compact_crops,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningLimitError,
    BlockPlanningMode,
    MatrixLocalIsland,
    MatrixLocalPlacement,
    MembershipUnit,
    MembershipUnitKind,
    RecognitionBlock,
)
from app.sparse_pipeline.contracts import Box, SegmentSpan
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrLane,
    OcrTransform,
    OcrWord,
)


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _fixture(
    *,
    two_islands: bool = False,
    empty_segment_id: str | None = None,
    unowned_gutter: bool = False,
) -> tuple[
    bytes,
    BlockPlan,
    np.ndarray,
    tuple[str, ...],
    tuple[SegmentSpan, ...],
    dict[str, Box],
]:
    segment_ids = ("segment-a", "segment-b", "segment-c", "segment-d")
    bboxes = {
        "segment-a": Box(2, 2, 10, 8),
        "segment-b": Box(18, 2, 26, 8),
        "segment-c": Box(2, 14, 10, 20),
        "segment-d": Box(18, 14, 26, 20),
    }
    image = Image.new("RGB", (32, 24), (255, 255, 255))
    ownership = np.full((24, 32), -1, dtype=np.int32)
    for label, segment_id in enumerate(segment_ids):
        bbox = bboxes[segment_id]
        ownership[bbox.top : bbox.bottom, bbox.left : bbox.right] = label
        if segment_id != empty_segment_id:
            for y in range(bbox.top, bbox.bottom):
                for x in range(bbox.left, bbox.right):
                    image.putpixel((x, y), (20 + label * 20,) * 3)
    if unowned_gutter:
        bbox = bboxes["segment-a"]
        ownership[bbox.top : bbox.bottom, bbox.left : bbox.left + 4] = -1

    if two_islands:
        islands = (
            MatrixLocalIsland(
                local_region_id="local-region-000000",
                island_id="island-000000",
                segment_ids=segment_ids[:2],
                region_boundary=(0, 2),
                matrix_segment_shape=(1, 2),
            ),
            MatrixLocalIsland(
                local_region_id="local-region-000001",
                island_id="island-000001",
                segment_ids=segment_ids[2:],
                region_boundary=(2, 4),
                matrix_segment_shape=(1, 2),
            ),
        )
        positions = ((0, 0), (0, 1), (0, 0), (0, 1))
        matrix_positions = positions
        island_indexes = (0, 0, 1, 1)
        region_orders = (0, 1, 0, 1)
        window_kind = "polar-local-signature"
    else:
        islands = (
            MatrixLocalIsland(
                local_region_id="local-region-000000",
                island_id="island-000000",
                segment_ids=segment_ids,
                region_boundary=(0, 8),
                matrix_segment_shape=(2, 4),
            ),
        )
        positions = ((0, 0), (0, 2), (1, 0), (1, 3))
        matrix_positions = positions
        island_indexes = (0, 0, 0, 0)
        region_orders = (0, 2, 4, 7)
        window_kind = "polar-local-full"
    placements = tuple(
        MatrixLocalPlacement(
            segment_id=segment_id,
            local_region_id=islands[island_index].local_region_id,
            island_id=islands[island_index].island_id,
            polar_order=(0 if island_index == 0 else 2) + region_order,
            region_order=region_order,
            table_row=positions[index][0],
            table_column=positions[index][1],
            matrix_row=matrix_positions[index][0],
            matrix_column=matrix_positions[index][1],
        )
        for index, (segment_id, island_index, region_order) in enumerate(
            zip(segment_ids, island_indexes, region_orders)
        )
    )
    block = RecognitionBlock(
        block_id="block-000000",
        bbox=Box(0, 0, 32, 24),
        core_segment_ids=segment_ids,
        segment_ids=segment_ids,
        context_segment_ids=(),
        object_ids=("object-000000",),
        scope_id="scope-000000",
        matrix_window=(0, 2, 0, 4),
        matrix_window_kind=window_kind,
        matrix_segment_shape=(2, 4),
        local_islands=islands,
        local_placements=placements,
    )
    units = (
        MembershipUnit(
            unit_id="membership-unit-000000",
            kind=MembershipUnitKind.SUBBLOCK,
            segment_ids=segment_ids,
            block_ids=(block.block_id,),
            scope_id=block.scope_id,
        ),
    )
    plan = BlockPlan(
        aligned_size=(32, 24),
        source_segment_ids=segment_ids,
        blocks=(block,),
        adjacent_algebra=(),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=units,
        matrix_sha256="a" * 64,
    )
    spans = tuple(
        SegmentSpan(
            segment_id,
            placements[index].table_row,
            placements[index].table_row + 1,
            placements[index].table_column,
            placements[index].table_column + 1,
        )
        for index, segment_id in enumerate(segment_ids)
    )
    return (
        _png(image),
        plan,
        ownership,
        segment_ids,
        spans,
        bboxes,
    )


def _render(**kwargs: object):
    page, plan, ownership, segment_ids, spans, bboxes = _fixture(**kwargs)
    crops, compactions = build_deferred_compact_crops(
        page,
        aligned_size=(32, 24),
        plan=plan,
        ownership=ownership,
        ownership_segment_ids=segment_ids,
        segment_spans=spans,
        segment_bboxes=bboxes,
    )
    return crops[0], compactions[0], plan.blocks[0], bboxes


def test_local_full_preserves_rows_holes_mapping_and_digest() -> None:
    crop, compaction, block, bboxes = _render()
    repeated, repeated_compaction, _block, _bboxes = _render()
    by_segment = {placement.segment_ids[0]: placement for placement in compaction.placements}

    assert len(compaction.placements) == 4
    assert hashlib.sha256(crop.raw.png_bytes).hexdigest() == hashlib.sha256(repeated.raw.png_bytes).hexdigest()
    assert compaction == repeated_compaction
    assert compaction.raster_kind is CompactionRasterKind.CANONICAL_LOCALITY
    assert compaction.raw_sha256 == hashlib.sha256(crop.raw.png_bytes).hexdigest()
    assert by_segment["segment-a"].crop_bbox.top == by_segment["segment-b"].crop_bbox.top
    assert by_segment["segment-c"].crop_bbox.top == by_segment["segment-d"].crop_bbox.top
    assert by_segment["segment-c"].crop_bbox.top > by_segment["segment-a"].crop_bbox.bottom
    assert by_segment["segment-c"].crop_bbox.left == 0
    for segment_id, placement in by_segment.items():
        assert placement.source_bbox == bboxes[segment_id]
        mapped = AdaptivePersistentOcrSession._map_word(
            OcrWord("token", placement.crop_bbox, 0.9),
            placement,
            block.bbox,
        )
        assert mapped.bbox == bboxes[segment_id]


def test_local_full_omits_empty_position_without_fake_tile_or_row_glue() -> None:
    _crop, compaction, _block, _bboxes = _render(empty_segment_id="segment-b")
    by_segment = {placement.segment_ids[0]: placement for placement in compaction.placements}

    assert set(by_segment) == {"segment-a", "segment-c", "segment-d"}
    assert compaction.omitted_empty_units == ("membership-unit-000000",)
    assert by_segment["segment-c"].crop_bbox.top == by_segment["segment-a"].crop_bbox.top
    assert by_segment["segment-c"].crop_bbox.left > by_segment["segment-a"].crop_bbox.right
    assert by_segment["segment-d"].crop_bbox.top > by_segment["segment-a"].crop_bbox.bottom
    assert by_segment["segment-d"].crop_bbox.left == 0


def test_giant_overlap_chain_uses_only_bounded_block_local_slots() -> None:
    block_count = 130
    private_ids = tuple(f"private-{index:03d}" for index in range(block_count))
    overlap_ids = tuple(f"overlap-{index:03d}" for index in range(block_count - 1))
    all_unit_ids = (*private_ids, *overlap_ids)
    source_bboxes = {unit_id: Box(index * 10, 0, index * 10 + 8, 6) for index, unit_id in enumerate(all_unit_ids)}
    covered: set[str] = set()
    source_boxes_by_unit: dict[str, set[Box]] = {}
    crop_boxes_by_unit: dict[str, list[Box]] = {}
    block_members: list[set[str]] = []

    for block_index in range(block_count):
        member_ids = (
            *((overlap_ids[block_index - 1],) if block_index else ()),
            private_ids[block_index],
            *((overlap_ids[block_index],) if block_index + 1 < block_count else ()),
        )
        block_members.append(set(member_ids))
        placements = tuple(
            MatrixLocalPlacement(
                segment_id=unit_id,
                local_region_id="local-region-000000",
                island_id="island-000000",
                polar_order=index,
                region_order=index,
                table_row=0,
                table_column=index,
                matrix_row=0,
                matrix_column=index,
            )
            for index, unit_id in enumerate(member_ids)
        )
        block = RecognitionBlock(
            block_id=f"block-{block_index:06d}",
            bbox=Box(0, 0, 32, 8),
            core_segment_ids=member_ids,
            segment_ids=member_ids,
            context_segment_ids=(),
            object_ids=("object-000000",),
            scope_id="scope-000000",
            matrix_window=(0, 1, 0, len(member_ids)),
            matrix_window_kind="polar-local-full",
            matrix_segment_shape=(1, len(member_ids)),
            local_islands=(
                MatrixLocalIsland(
                    local_region_id="local-region-000000",
                    island_id="island-000000",
                    segment_ids=member_ids,
                    region_boundary=(0, len(member_ids)),
                    matrix_segment_shape=(1, len(member_ids)),
                ),
            ),
            local_placements=placements,
        )
        tiles = tuple(
            _Tile(
                unit_id=unit_id,
                segment_ids=(unit_id,),
                source_bbox=source_bboxes[unit_id],
                pixels=np.zeros((6, 8, 3), dtype=np.uint8),
            )
            for unit_id in member_ids
        )
        slots, slot_shape = _bounded_locality_family_slots(tiles, block)
        rendered = _render_canonical_locality_raster(
            tiles,
            block,
            membership_slot_by_id=slots,
            slot_shape=slot_shape,
        )

        assert len(slots) == len(tiles)
        assert set(slots.values()) == set(range(len(tiles)))
        assert max(slot_shape) <= 16
        assert rendered.packed_canvas_pixels <= 16_000_000
        assert {item.unit_id for item in rendered.placements} == set(member_ids)
        covered.update(item.unit_id for item in rendered.placements)
        for item in rendered.placements:
            source_boxes_by_unit.setdefault(item.unit_id, set()).add(item.source_bbox)
            crop_boxes_by_unit.setdefault(item.unit_id, []).append(item.crop_bbox)

    assert covered == set(all_unit_ids)
    assert all(len(boxes) == 1 for boxes in source_boxes_by_unit.values())
    assert any(len(set(crop_boxes_by_unit[unit_id])) > 1 for unit_id in overlap_ids)
    for index, overlap_id in enumerate(overlap_ids):
        first = block_members[index]
        second = block_members[index + 1]
        assert first & second == {overlap_id}
        assert first ^ second == (first | second) - {overlap_id}


def test_local_full_preserves_raw_gutter_inside_exact_segment_bbox() -> None:
    crop, compaction, _block, _bboxes = _render(unowned_gutter=True)
    placement = next(item for item in compaction.placements if item.segment_ids == ("segment-a",))
    with Image.open(io.BytesIO(crop.raw.png_bytes)) as rendered:
        rendered.load()
        pixel = rendered.convert("RGB").getpixel((placement.crop_bbox.left + 1, placement.crop_bbox.top + 1))

    assert max(pixel) < 100


def test_two_island_signature_has_adaptive_visual_separator() -> None:
    _crop, compaction, _block, _bboxes = _render(two_islands=True)
    by_segment = {placement.segment_ids[0]: placement for placement in compaction.placements}
    first_bottom = max(by_segment[segment_id].crop_bbox.bottom for segment_id in ("segment-a", "segment-b"))
    second_top = min(by_segment[segment_id].crop_bbox.top for segment_id in ("segment-c", "segment-d"))
    tile_height = by_segment["segment-a"].crop_bbox.bottom - by_segment["segment-a"].crop_bbox.top

    assert second_top - first_bottom >= tile_height
    assert by_segment["segment-a"].crop_bbox.top == by_segment["segment-b"].crop_bbox.top
    assert by_segment["segment-c"].crop_bbox.top == by_segment["segment-d"].crop_bbox.top


def test_local_layout_pixel_limit_fails_without_shrinking_tiles() -> None:
    _crop, _compaction, block, _bboxes = _render()
    tiles = (
        _Tile(
            "unit-0",
            ("segment-a",),
            Box(0, 0, 20, 20),
            np.zeros((20, 20, 3), dtype=np.uint8),
        ),
        _Tile(
            "unit-1",
            ("segment-b",),
            Box(20, 0, 40, 20),
            np.zeros((20, 20, 3), dtype=np.uint8),
        ),
    )

    with pytest.raises(BlockPlanningLimitError, match="pixel limit"):
        _pack_local_tiles(tiles, block, maximum_pixels=100)


def test_canonical_locality_tiles_cannot_use_generic_repacking() -> None:
    crop, compaction, _block, _bboxes = _render()
    with Image.open(io.BytesIO(crop.raw.png_bytes)) as opened:
        opened.load()
        image = opened.convert("RGB")
    try:
        tiles = tuple(
            _Tile(
                placement.unit_id,
                placement.segment_ids,
                placement.source_bbox,
                np.asarray(image.crop(placement.crop_bbox.as_tuple())).copy(),
                placement.crop_bbox,
                crop.raw.png_bytes,
            )
            for placement in compaction.placements
        )
    finally:
        image.close()

    with pytest.raises(ValueError, match="canonical locality tiles"):
        _pack_tiles(tiles)

    session = object.__new__(AdaptivePersistentOcrSession)
    group = session._build_context_group(
        block_id="block-000000",
        depth=0,
        first_order=0,
        tiles=tiles,
    )
    assert hashlib.sha256(group.png_bytes).hexdigest() == hashlib.sha256(crop.raw.png_bytes).hexdigest()
    assert group.placements == compaction.placements


@dataclass(frozen=True)
class _Config:
    languages: tuple[str, ...]


class _RecordingWorker:
    payloads: list[bytes] = []

    def __init__(self, *, config: _Config) -> None:
        self.config = config

    def recognize(self, png_bytes: bytes) -> OcrEngineOutput:
        type(self).payloads.append(png_bytes)
        with Image.open(io.BytesIO(png_bytes)) as opened:
            opened.load()
            width, height = opened.size
            first_pixel = opened.convert("RGB").getpixel((0, 0))
        text = "gamma97" if first_pixel == (1, 2, 3) else "raw中96"
        return OcrEngineOutput(
            text=text,
            words=(OcrWord(text, Box(0, 0, width, height), 0.90),),
            geometry=OcrOutputGeometry.WORD_BOXES,
        )

    def close(self) -> None:
        pass


class _RecordingEnhancer:
    def __init__(self) -> None:
        self.sources: list[bytes] = []

    def enhance_many(self, crops):
        outputs = []
        for item in crops:
            self.sources.append(item.png_bytes)
            with Image.open(io.BytesIO(item.png_bytes)) as opened:
                opened.load()
                image = opened.convert("RGB")
            try:
                image.putpixel((0, 0), (1, 2, 3))
                outputs.append(SimpleNamespace(png_bytes=_png(image)))
            finally:
                image.close()
        return tuple(outputs)


def test_direct_and_adaptive_queue_share_canonical_locality_raster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crop, compaction, block, bboxes = _render()
    direct_digest = hashlib.sha256(crop.raw.png_bytes).hexdigest()
    _RecordingWorker.payloads = []
    monkeypatch.setattr(
        "app.sparse_pipeline.adaptive_language_ocr.assess_grammar",
        lambda output, _languages: GrammarAssessment(
            int(output.text[-2:]),
            output.text.endswith("97"),
            (),
        ),
    )
    lane = OcrLane(
        lane_id="canonical-locality-test",
        resource=OcrResource.CPU,
        max_workers=1,
        worker_factory=lambda: _RecordingWorker(config=_Config(("rus", "eng"))),
    )
    enhancer = _RecordingEnhancer()
    session = AdaptivePersistentOcrSession((lane,))
    session._enhancer = enhancer
    plan = BlockPlan(
        aligned_size=(32, 24),
        source_segment_ids=block.segment_ids,
        blocks=(block,),
        adjacent_algebra=(),
        mode=BlockPlanningMode.SPATIAL_2D,
        membership_units=(
            MembershipUnit(
                unit_id="membership-unit-000000",
                kind=MembershipUnitKind.SUBBLOCK,
                segment_ids=block.segment_ids,
                block_ids=(block.block_id,),
                scope_id=block.scope_id,
            ),
        ),
        matrix_sha256="a" * 64,
    )
    try:
        queue = session.run_with_compaction(
            plan=plan,
            crops=(crop,),
            compactions=(compaction,),
        )
    finally:
        session.close()

    assert hashlib.sha256(_RecordingWorker.payloads[0]).hexdigest() == (direct_digest)
    assert enhancer.sources == [crop.raw.png_bytes]
    with Image.open(io.BytesIO(_RecordingWorker.payloads[1])) as gamma:
        gamma.load()
        assert gamma.size == (crop.bbox.width, crop.bbox.height)
    assert queue.jobs[0].transform is OcrTransform.GAMMA
    assert queue.jobs[0].context_sha256 == direct_digest
    for placement in compaction.placements:
        mapped = AdaptivePersistentOcrSession._map_word(
            OcrWord("token", placement.crop_bbox, 0.9),
            placement,
            block.bbox,
        )
        assert mapped.bbox == bboxes[placement.segment_ids[0]]

    invalid = replace(
        compaction,
        raster_kind=CompactionRasterKind.SPATIAL_PACKED,
    )
    session = AdaptivePersistentOcrSession((lane,))
    try:
        with pytest.raises(ValueError, match="canonical renderer"):
            session.run_with_compaction(
                plan=plan,
                crops=(crop,),
                compactions=(invalid,),
            )
    finally:
        session.close()
