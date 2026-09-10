from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.ocr_fusion import OcrFusionLimitError
from app.sparse_pipeline.ocr_queue import OcrJobStatus


@pytest.fixture(scope="module")
def recognition() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_object_recognition.py"
    name = "_debug_object_recognition_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _stored_table(tmp_path: Path) -> tuple[Path, Path]:
    object_dir = tmp_path / "06-objects" / "objects" / "object-000000"
    geometry_dir = tmp_path / "01-geometry"
    object_dir.mkdir(parents=True)
    geometry_dir.mkdir(parents=True)
    segment_ids = tuple(f"table-{row}-{column}" for row in range(3) for column in range(2))
    (object_dir / "object.json").write_text(
        json.dumps(
            {
                "object_id": "object-000000",
                "kind": "table",
                "bbox": [0, 0, 200, 120],
                "segment_ids": list(segment_ids),
                "evidence": ["test-table"],
            }
        ),
        encoding="utf-8",
    )
    rows = []
    for row in range(3):
        rows.append(
            {
                "row": row,
                "matrix_y": [row * 40, (row + 1) * 40],
                "y": [row * 40, (row + 1) * 40],
                "compressed_codes": [0 if row == 0 else 3, 5 if row == 0 else 8, None],
                "null_tail": {
                    "from_column": 2,
                    "repeat_last_code": 5 if row == 0 else 8,
                    "through_column_exclusive": 2,
                },
                "segments": [
                    {
                        "column": column,
                        "code": (0 if (row, column) == (0, 0) else 5 if row == 0 else 3 if column == 0 else 8),
                        "state": "payload",
                        "source_segment_ids": [f"table-{row}-{column}"],
                        "matrix_x": [column * 100, (column + 1) * 100],
                        "x": [column * 100, (column + 1) * 100],
                    }
                    for column in range(2)
                ],
            }
        )
    (object_dir / "matrix.json").write_text(
        json.dumps(
            {
                "schema": "object-local-sparse-topology-v1",
                "object_id": "object-000000",
                "kind": "table",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )
    Image.new("RGB", (200, 120), "white").save(object_dir / "object.png")
    records = []
    for row in range(3):
        for column in range(2):
            records.append(
                {
                    "segment_id": f"table-{row}-{column}",
                    "bbox": {
                        "left": column * 100,
                        "top": row * 40,
                        "right": (column + 1) * 100,
                        "bottom": (row + 1) * 40,
                    },
                    "source_bbox": {
                        "left": column * 100,
                        "top": row * 40,
                        "right": (column + 1) * 100,
                        "bottom": (row + 1) * 40,
                    },
                    "kind": "text",
                    "ink_pixels": 100,
                    "row_index": row,
                    "order_key": [row, column],
                    "parent_path": ["test-root"],
                    "component_ids": [row * 2 + column],
                }
            )
    (geometry_dir / "segments.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n",
        encoding="utf-8",
    )
    return object_dir, geometry_dir / "segments.jsonl"


def test_saved_object_matrix_drives_real_overlapping_planner(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path)
    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )
    plan = recognition.matrix_orxor_plan(stored)

    assert stored.matrix.segment_ids() == frozenset(item.segment_id for item in stored.segments)
    memberships = tuple(item.segment_ids for item in plan.blocks)
    assert all(len(segment_ids) >= 2 for segment_ids in memberships)
    assert {segment_id for segment_ids in memberships for segment_id in segment_ids} == {
        item.segment_id for item in stored.segments
    }
    signatures = {
        segment.segment_id: tuple(segment.segment_id in segment_ids for segment_ids in memberships)
        for segment in stored.segments
    }
    assert len(set(signatures.values())) == len(stored.segments)
    segment_by_id = {segment.segment_id: segment for segment in stored.segments}
    assert len({item.bbox.as_tuple() for item in plan.blocks}) > 1
    assert all(
        item.bbox == Box.union(segment_by_id[segment_id].bbox for segment_id in item.segment_ids)
        for item in plan.blocks
    )
    signatures = {
        segment_id: tuple(index for index, block in enumerate(plan.blocks) if segment_id in block.segment_ids)
        for segment_id in plan.source_segment_ids
    }
    assert all(signatures.values())
    assert len(set(signatures.values())) == len(signatures)
    assert {item.matrix_segment_shape for item in plan.blocks} == {
        (1, 2),
        (2, 2),
    }
    assert len(plan.membership_units) == 6
    assert all(len(item.segment_ids) == 1 for item in plan.membership_units)
    assert plan.adjacent_algebra
    assert {item.matrix_window_kind for item in plan.blocks} == {"dyadic-mask"}
    assert "matrix-table-dyadic-codes=" "scope-000000:units=6,bits=3,context-width=1" in plan.diagnostics


def test_whole_object_is_a_single_context_control(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path)
    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )
    plan = recognition.whole_object_plan(stored)

    assert len(plan.blocks) == 1
    assert plan.blocks[0].bbox.as_tuple() == (0, 0, 200, 120)
    assert plan.blocks[0].segment_ids == tuple(item.segment_id for item in stored.segments)
    assert plan.membership_units[0].kind.value == "subblock"


def test_line_window_ab_uses_overlapping_rows_without_singletons(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path)
    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )
    stored = replace(stored, source_kind="paragraph")

    plan = recognition.line_windows_plan(stored)

    assert tuple(item.segment_ids for item in plan.blocks) == (
        ("table-0-0", "table-0-1", "table-1-0", "table-1-1"),
        ("table-1-0", "table-1-1", "table-2-0", "table-2-1"),
    )
    assert all(len(item.segment_ids) > 1 for item in plan.blocks)
    assert plan.adjacent_algebra[0].intersection_segment_ids == (
        "table-1-0",
        "table-1-1",
    )
    assert tuple(item.segment_ids for item in plan.membership_units) == (
        ("table-0-0", "table-0-1"),
        ("table-1-0", "table-1-1"),
        ("table-2-0", "table-2-1"),
    )
    assert recognition._object_policies(stored) == (
        "whole-object",
        "line-windows",
    )
    assert recognition._object_policies(replace(stored, source_kind="list")) == ("whole-object", "line-windows")
    assert recognition._object_policies(replace(stored, source_kind="table")) == ("matrix-orxor",)
    assert recognition._object_policies(replace(stored, source_kind="flow")) == ("fixed-flow",)


def test_lazy_selection_keeps_stronger_computed_whole_object_evidence(
    recognition: ModuleType,
) -> None:
    source_segment_ids = ("segment-0", "segment-1", "segment-2")

    def policy_run(
        policy: str,
        *,
        text: str,
        grammar: int,
        fusion_status: str = "complete",
        unresolved_units: int = 0,
        covered_segment_ids: tuple[str, ...] = source_segment_ids,
    ) -> SimpleNamespace:
        block = SimpleNamespace(
            block_id=f"{policy}-block",
            segment_ids=covered_segment_ids,
        )
        output = SimpleNamespace(
            text=text,
            words=(SimpleNamespace(confidence=0.95),),
        )
        job = SimpleNamespace(
            block_id=block.block_id,
            status=OcrJobStatus.COMPLETE,
            output=output,
        )
        return SimpleNamespace(
            policy=policy,
            plan=SimpleNamespace(
                blocks=(block,),
                source_segment_ids=source_segment_ids,
                membership_units=(
                    SimpleNamespace(unit_id="unit-0"),
                    SimpleNamespace(unit_id="unit-1"),
                    SimpleNamespace(unit_id="unit-2"),
                ),
            ),
            queue=SimpleNamespace(
                jobs=(job,),
                diagnostics=(f"block={block.block_id};grammar={grammar};transform=raw",),
            ),
            result_text=text,
            unresolved_units=unresolved_units,
            fusion_status=fusion_status,
        )

    stored = SimpleNamespace(source_kind="list")
    whole_object = policy_run(
        "whole-object",
        text="Полный исходный текст с сохранённым содержанием",
        grammar=94,
    )
    line_windows = policy_run(
        "line-windows",
        text="Потерянный текст",
        grammar=100,
        covered_segment_ids=("segment-0", "segment-1"),
    )

    selected = recognition.select_paragraph_list_policy_run(
        stored,
        {
            whole_object.policy: whole_object,
            line_windows.policy: line_windows,
        },
    )

    assert recognition.lazy_paragraph_list_fallback_reason(whole_object) is None
    assert selected is whole_object
    payload = recognition._paragraph_list_selection_payload(
        stored,
        {
            whole_object.policy: whole_object,
            line_windows.policy: line_windows,
        },
    )
    assert payload["selected_policy"] == "whole-object"
    assert payload["policies"]["whole-object"]["evidence_score"] > payload["policies"]["line-windows"]["evidence_score"]
    assert recognition._object_policies(
        stored,
        paragraph_list_ab=True,
    ) == ("whole-object", "line-windows")

    failed_whole_object = policy_run(
        "whole-object",
        text="Полный исходный текст с сохранённым содержанием",
        grammar=94,
        fusion_status="failed",
    )
    assert (
        recognition.select_paragraph_list_policy_run(
            stored,
            {
                failed_whole_object.policy: failed_whole_object,
                line_windows.policy: line_windows,
            },
        )
        is line_windows
    )

    empty_whole_object = policy_run(
        "whole-object",
        text="",
        grammar=100,
    )
    assert recognition.lazy_paragraph_list_fallback_reason(empty_whole_object) == "empty-evidence"


def test_saved_table_crop_may_retain_structural_margin(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path)
    record = json.loads((object_dir / "object.json").read_text(encoding="utf-8"))
    record["bbox"] = [0, 0, 220, 140]
    (object_dir / "object.json").write_text(
        json.dumps(record),
        encoding="utf-8",
    )
    Image.new("RGB", (220, 140), "white").save(object_dir / "object.png")

    shifted = []
    for line in segments_path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        for key in ("bbox", "source_bbox"):
            item[key] = {axis: value + 10 for axis, value in item[key].items()}
        shifted.append(item)
    segments_path.write_text(
        "\n".join(json.dumps(item) for item in shifted) + "\n",
        encoding="utf-8",
    )

    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )
    plan = recognition.matrix_orxor_plan(stored)

    assert stored.aligned_size == (220, 140)
    assert stored.objects_result.objects[0].bbox.as_tuple() == (10, 10, 210, 130)
    assert len(plan.blocks) == 4


def test_saved_matrix_row_gaps_remain_physical_whitespace(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path)
    payload = json.loads((object_dir / "matrix.json").read_text(encoding="utf-8"))
    payload["rows"][0]["matrix_y"] = [0, 35]
    payload["rows"][1]["matrix_y"] = [40, 75]
    payload["rows"][2]["matrix_y"] = [80, 120]
    (object_dir / "matrix.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )

    assert tuple((item.start, item.end) for item in stored.matrix.rows) == (
        (0, 40),
        (40, 80),
        (80, 120),
    )
    assert recognition.matrix_orxor_plan(stored).blocks


def test_matrix_adapter_fails_closed_when_owned_segment_is_missing(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, _ = _stored_table(tmp_path)
    payload = json.loads((object_dir / "matrix.json").read_text(encoding="utf-8"))

    with pytest.raises(
        recognition.ObjectRecognitionInvariantError,
        match="represent every owned source segment",
    ):
        recognition.object_matrix_to_sparse(
            payload,
            source_segment_ids=(
                "table-0-0",
                "table-0-1",
                "table-1-0",
                "table-1-1",
                "table-2-0",
                "table-2-1",
                "missing-segment",
            ),
        )


def test_canonical_object_tree_contains_only_requested_files(
    recognition: ModuleType,
    tmp_path: Path,
) -> None:
    object_dir, segments_path = _stored_table(tmp_path / "input")
    stored = recognition.load_stored_object(
        object_dir,
        segments_path=segments_path,
    )
    plan = recognition.matrix_orxor_plan(stored)
    output = tmp_path / "output" / "object-000000"
    (output / "blocks").mkdir(parents=True)
    run = SimpleNamespace(
        policy="matrix-orxor",
        plan=plan,
        crops=tuple(
            SimpleNamespace(
                block_id=block.block_id,
                raw=SimpleNamespace(png_bytes=b"stored-block-png"),
            )
            for block in plan.blocks
        ),
        queue=SimpleNamespace(jobs=()),
        segment_lines=("table-0-0\ttext",),
        result_text="text",
    )

    recognition._write_canonical_object_artifacts(output, run=run)

    assert {item.name for item in output.iterdir()} == {
        "blocks",
        "segments.txt",
        "result.txt",
    }
    blocks_dir = output / "blocks"
    assert (blocks_dir / "manifest.json").is_file()
    for block in (item for item in blocks_dir.iterdir() if item.is_dir()):
        assert {item.name for item in block.iterdir()} == {
            "image.png",
            "recognized.txt",
            "block.json",
        }


@pytest.mark.parametrize(
    ("reference", "recognized"),
    (
        ("", ""),
        ("", "текст"),
        ("a b\t中\n", "ab中"),
        ("kitten", "sitting"),
        ("А" * 1_500 + "x" + "中" * 300, "Б" * 1_500 + "y" + "中" * 300),
    ),
)
def test_distance_only_metric_is_bit_exact_with_full_alignment(
    recognition: ModuleType,
    reference: str,
    recognized: str,
) -> None:
    assert recognition._metric(reference, recognized) == recognition._metric(
        reference,
        recognized,
        debug_full_alignment=True,
    )


def test_long_metric_uses_bit_vector_without_full_alignment(
    recognition: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_full_alignment(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("full edit script must be debug-only")

    monkeypatch.setattr(recognition, "align_exact_text", reject_full_alignment)

    assert recognition._metric("А" * 13_561, "Б" * 13_561) == {
        "lost_characters": 13_561,
        "reference_characters": 13_561,
        "recognized_characters": 13_561,
        "accuracy_percent": 0.0,
    }


def test_full_metric_alignment_is_explicit_and_bounded(
    recognition: ModuleType,
) -> None:
    args = recognition._parser().parse_args(["--debug-full-metric-alignment"])

    assert args.debug_full_metric_alignment is True
    with pytest.raises(OcrFusionLimitError):
        recognition._metric(
            "А" * 2_000,
            "Б" * 2_000,
            debug_full_alignment=True,
        )


def test_compact_atlas_uses_original_block_bbox_for_fusion(
    recognition: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = SimpleNamespace(
        block_id="block-000000",
        bbox=recognition.Box(40, 60, 140, 180),
        segment_ids=("segment-0",),
    )
    plan = SimpleNamespace(
        blocks=(block,),
        membership_units=(
            SimpleNamespace(
                unit_id="membership-unit-000000",
                kind=recognition.MembershipUnitKind.SEGMENT,
                segment_ids=("segment-0",),
            ),
        ),
    )
    png_stream = recognition.io.BytesIO()
    recognition.Image.new("RGB", (32, 24), "white").save(
        png_stream,
        format="PNG",
        dpi=(300, 300),
    )
    png_bytes = png_stream.getvalue()
    crop = recognition.BlockCropPair(
        block_id=block.block_id,
        bbox=recognition.Box(0, 0, 32, 24),
        segment_ids=block.segment_ids,
        raw=recognition.CropInput(
            "block-000000-raw",
            png_bytes,
        ),
        gamma=None,
    )
    separated = recognition.SeparatedBlocks(
        policy="matrix-orxor",
        plan=plan,
        crops=(crop,),
        planning_seconds=0.0,
        crop_seconds=0.0,
        compactions=(
            recognition.BlockCompaction(
                block_id=block.block_id,
                placements=(),
                omitted_empty_units=(),
                occupied_pixels_before=1,
                occupied_pixels_after=1,
                packed_canvas_pixels=1,
            ),
        ),
    )
    block_ocr = recognition.BlockOcrResult(
        separated=separated,
        queue=SimpleNamespace(),
        ocr_seconds=0.0,
    )
    stored = SimpleNamespace(
        segments=(SimpleNamespace(segment_id="segment-0"),),
    )

    class Fusion:
        def fuse(self, *, plan, segments, crops, queue):
            assert crops[0].bbox == block.bbox
            assert crops[0].segment_ids == crop.segment_ids
            assert crops[0].raw.png_bytes is png_bytes
            assert crop.bbox.as_tuple() == (0, 0, 32, 24)
            return SimpleNamespace(
                segments=(
                    SimpleNamespace(
                        segment_id="segment-0",
                        selected_text="recognized",
                        unresolved=False,
                    ),
                ),
                segment_groups=(),
                status=SimpleNamespace(value="complete"),
            )

    monkeypatch.setattr(
        recognition,
        "_best_job_by_block",
        lambda _queue: {block.block_id: object()},
    )
    monkeypatch.setattr(
        recognition,
        "OcrEvidenceFusion",
        lambda _config: Fusion(),
    )

    run = recognition.get_segments(stored, block_ocr)

    assert run.fusion_status == "complete"
    assert run.result_text == "recognized"
