from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image


@pytest.fixture(scope="module")
def lab() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_object_local_labs.py"
    name = "_debug_object_local_labs_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _object(bundle: object, kind: str) -> object:
    return next(item for item in bundle.objects.objects if item.kind.value == kind)


def test_table_object_slice_produces_actual_abc_and_recovers_six_segments(
    lab: ModuleType,
) -> None:
    bundle = lab.synthetic_frozen_bundle()
    table = _object(bundle, "table")
    plan, matrix = lab.plan_one_object(bundle, table)

    assert matrix.segment_ids() == frozenset(table.segment_ids)
    assert tuple(item.segment_ids for item in plan.blocks) == (
        (
            "table-0-0",
            "table-0-1",
            "table-1-0",
            "table-1-1",
        ),
        (
            "table-1-0",
            "table-1-1",
            "table-2-0",
            "table-2-1",
        ),
        ("table-0-0", "table-1-0", "table-2-0"),
    )
    decoded = lab._decode_oracle_membership(plan.blocks, table.segment_ids)
    assert lab._membership_diff(table.segment_ids, decoded)["status"] == "exact"
    assert all(len(item.segment_ids) == 1 for item in plan.membership_units)
    assert len(plan.adjacent_algebra) == 3


def test_cross_object_block_is_forbidden(lab: ModuleType) -> None:
    bundle = lab.synthetic_frozen_bundle()
    paragraph = _object(bundle, "paragraph")
    forged = SimpleNamespace(
        block_id="forged-cross-object",
        segment_ids=(paragraph.segment_ids[0], "list-0-0"),
        object_ids=("object-000000", "object-000001"),
    )
    with pytest.raises(lab.LabInvariantError, match="crosses object"):
        lab.assert_object_local_blocks(paragraph, (forged,))


def test_whole_paragraph_policy_never_splits_object(lab: ModuleType) -> None:
    bundle = lab.synthetic_frozen_bundle()
    paragraph = _object(bundle, "paragraph")
    blocks = lab.paragraph_blocks(bundle, paragraph, policy="whole-object")

    assert len(blocks) == 1
    assert blocks[0].segment_ids == paragraph.segment_ids
    assert blocks[0].bbox == paragraph.bbox


def test_list_item_policy_preserves_every_sparse_item_boundary(
    lab: ModuleType,
) -> None:
    bundle = lab.synthetic_frozen_bundle()
    document_list = _object(bundle, "list")
    blocks = lab.list_item_blocks(bundle, document_list)

    assert tuple(item.segment_ids for item in blocks) == lab._rows_for_object(document_list, bundle.matrix)
    assert len(blocks) == 3
    assert all(len(item.segment_ids) == 2 for item in blocks)
    assert not any(
        set(first.segment_ids) & set(second.segment_ids)
        for index, first in enumerate(blocks)
        for second in blocks[index + 1 :]
    )


def test_orxor_lab_never_calls_assembly(
    lab: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenAssembler:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("assembly was called from OR/XOR lab")

    monkeypatch.setattr(lab, "DocumentAssembler", ForbiddenAssembler)
    summary = lab.run_orxor_lab(lab.synthetic_frozen_bundle(), tmp_path / "orxor")

    assert summary["status"] == "exact"
    diff = json.loads((tmp_path / "orxor" / "object-000002" / "diff.json").read_text(encoding="utf-8"))
    assert diff["lost_segment_ids"] == []
    assert diff["extra_segment_ids"] == []
    assert diff["merged_units"] == []


def test_assembly_only_calls_ready_production_core_without_planner_or_replay(
    lab: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_plan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Stage 5 planner was called from assembly-only lab")

    def forbidden_assemble(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("end-to-end provenance replay was called")

    monkeypatch.setattr(lab.OverlappingBlockPlanner, "plan", forbidden_plan)
    monkeypatch.setattr(lab.DocumentAssembler, "assemble", forbidden_assemble)
    bundle = lab.synthetic_frozen_bundle()
    summary = lab.run_assembly_only_lab(bundle, tmp_path / "assembly")

    assert summary["status"] == "exact"
    assert (tmp_path / "assembly" / "document.txt").read_text(encoding="utf-8") == bundle.expected_document_text
    assert (tmp_path / "assembly" / "document.md").read_text(encoding="utf-8") == bundle.expected_document_markdown
    assert "Stage5.OverlappingBlockPlanner" in summary["skipped"]
    assert "Stage2.OcrEvidenceFusion" in summary["skipped"]


def test_granularity_lab_is_topology_only_and_real_ocr_is_fail_closed(
    lab: ModuleType,
    tmp_path: Path,
) -> None:
    summary = lab.run_granularity_lab(lab.synthetic_frozen_bundle(), tmp_path / "granularity")

    assert summary["status"] == "topology-exact-ocr-pending"
    assert all(summary["invariants"].values())
    assert summary["decision"]["status"] == "pending-real-ocr"
    for strategy in summary["strategies"]:
        assert strategy["real_ocr"]["status"] == "pending"
        assert strategy["real_ocr"]["elapsed_seconds"] is None
        assert strategy["real_ocr"]["text_accuracy_percent"] is None


def test_bundle_round_trip_and_full_run_publish_actual_crops_immutably(
    lab: ModuleType,
    tmp_path: Path,
) -> None:
    bundle = lab.synthetic_frozen_bundle()
    bundle_root = tmp_path / "frozen"
    lab.write_frozen_bundle(bundle_root, bundle)
    loaded = lab.load_frozen_bundle(bundle_root)

    assert lab.frozen_bundle_sha256(loaded) == lab.frozen_bundle_sha256(bundle)
    output_root = tmp_path / "runs"
    destination = lab.run_labs(loaded, output_root=output_root, run_id="isolated-v1")
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["immutable"] is True
    assert manifest["status"] == "complete-with-pending-ocr"
    assert manifest["labs"]["orxor"]["status"] == "exact"
    assert manifest["labs"]["assembly"]["status"] == "exact"
    assert manifest["labs"]["granularity"]["status"] == ("topology-exact-ocr-pending")
    pngs = tuple(destination.rglob("*.png"))
    assert pngs
    assert not any("overlay" in item.name.lower() for item in pngs)
    block_pngs = tuple((destination / "orxor").rglob("block-*.png"))
    assert len(block_pngs) == 3
    assert len({item.read_bytes() for item in block_pngs}) == 3
    for path in pngs:
        with Image.open(path) as opened:
            assert opened.format == "PNG"
            assert opened.width > 0 and opened.height > 0

    with pytest.raises(FileExistsError):
        lab.run_labs(loaded, output_root=output_root, run_id="isolated-v1")


def test_assembly_only_fails_closed_when_ready_text_is_missing(
    lab: ModuleType,
    tmp_path: Path,
) -> None:
    bundle = lab.synthetic_frozen_bundle()
    values = list(bundle.segment_texts)
    values[0] = (values[0][0], None)
    incomplete = lab.FrozenBundle(
        bundle_id="missing-ready-text",
        aligned_size=bundle.aligned_size,
        page_png=bundle.page_png,
        segments=bundle.segments,
        matrix=bundle.matrix,
        objects=bundle.objects,
        segment_texts=tuple(values),
    )

    with pytest.raises(lab.LabInvariantError, match="needs ready text"):
        lab.run_assembly_only_lab(incomplete, tmp_path / "assembly")
