from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image


@pytest.fixture(scope="module")
def recognition() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "debug"
        / "debug_object_recognition.py"
    )
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
    segment_ids = tuple(
        f"table-{row}-{column}" for row in range(3) for column in range(2)
    )
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
                        "code": (
                            0
                            if (row, column) == (0, 0)
                            else 5
                            if row == 0
                            else 3
                            if column == 0
                            else 8
                        ),
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

    assert stored.matrix.segment_ids() == frozenset(
        item.segment_id for item in stored.segments
    )
    memberships = tuple(item.segment_ids for item in plan.blocks)
    assert all(len(segment_ids) >= 2 for segment_ids in memberships)
    assert {
        segment_id
        for segment_ids in memberships
        for segment_id in segment_ids
    } == {item.segment_id for item in stored.segments}
    signatures = {
        segment.segment_id: tuple(
            segment.segment_id in segment_ids
            for segment_ids in memberships
        )
        for segment in stored.segments
    }
    assert len(set(signatures.values())) == len(stored.segments)
    segment_by_id = {
        segment.segment_id: segment for segment in stored.segments
    }
    assert len({item.bbox.as_tuple() for item in plan.blocks}) > 1
    assert all(
        item.bbox
        == type(item.bbox).union(
            segment_by_id[segment_id].bbox
            for segment_id in item.segment_ids
        )
        for item in plan.blocks
    )
    signatures = {
        segment_id: tuple(
            index
            for index, block in enumerate(plan.blocks)
            if segment_id in block.segment_ids
        )
        for segment_id in plan.source_segment_ids
    }
    assert all(signatures.values())
    assert len(set(signatures.values())) == len(signatures)
    assert {item.matrix_segment_shape for item in plan.blocks} == {(1, 1)}
    assert len(plan.membership_units) == 6
    assert all(len(item.segment_ids) == 1 for item in plan.membership_units)
    assert plan.adjacent_algebra
    assert {
        item.matrix_window_kind for item in plan.blocks
    } == {"dyadic-mask"}
    assert (
        "matrix-table-dyadic-codes="
        "scope-000000:units=6,bits=3,context-width=1"
        in plan.diagnostics
    )


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
    assert plan.blocks[0].segment_ids == tuple(
        item.segment_id for item in stored.segments
    )
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
    assert recognition._object_policies(
        replace(stored, source_kind="list")
    ) == ("whole-object", "line-windows")
    assert recognition._object_policies(
        replace(stored, source_kind="table")
    ) == ("matrix-orxor",)
    assert recognition._object_policies(
        replace(stored, source_kind="flow")
    ) == ("fixed-flow",)


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
    assert len(plan.blocks) == 3


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
