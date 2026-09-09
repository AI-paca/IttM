from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def lab() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_v16_matrix_handoff_lab.py"
    name = "_debug_v16_matrix_handoff_lab_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("source_key", "segments", "shape", "source_sha256", "first_crop_sha256"),
    (
        (
            "000041",
            33,
            (42, 95),
            "e39ad3e7d7e2d83489f82c88b616c7a425d541ac077771c792c967d5438f137c",
            "4209041b671a3764e94278a19793bab5fa0ed798fbccfdeefa9de28fb2a697ea",
        ),
        (
            "09",
            113,
            (130, 22),
            "d446ce8dffd91f434b06280aba8884ad0abae0a984001320774d713945e9cf70",
            "57407eff70a05a0806e93b3af4f0f1fd6b30df8b3aca0f4b5b01af6aace71c70",
        ),
    ),
)
def test_literal_loader_preserves_frozen_identity_and_matrix_payload(
    lab: ModuleType,
    source_key: str,
    segments: int,
    shape: tuple[int, int],
    source_sha256: str,
    first_crop_sha256: str,
) -> None:
    snapshot, _metadata = lab.load_frozen_v16(lab.DEFAULT_INPUT_ROOT, source_key)

    assert snapshot.source_sha256 == source_sha256
    assert snapshot.matrix_shape == shape
    assert len(snapshot.records) == segments
    assert snapshot.segment_ids == tuple(f"leaf-{index:06d}" for index in range(segments))
    assert snapshot.records[0].source_crop_sha256 == first_crop_sha256
    assert snapshot.source_sha256 == lab.FROZEN_INPUT_DIGESTS[source_key]["source"]
    assert snapshot.recursion_sha256 == lab.FROZEN_INPUT_DIGESTS[source_key]["recursion"]
    assert snapshot.matrix_tsv_sha256 == lab.FROZEN_INPUT_DIGESTS[source_key]["matrix_tsv"]
    assert len(snapshot.x_tracks) == shape[1]
    assert all(
        len(value) == 64
        for value in (
            snapshot.recursion_sha256,
            snapshot.matrix_tsv_sha256,
        )
    )


@pytest.mark.parametrize(
    ("source_key", "group_count", "gap_count"),
    (("000041", 10, 9), ("09", 18, 17)),
)
def test_serialized_group_boundary_replay_is_exact_but_not_object_detection(
    lab: ModuleType,
    source_key: str,
    group_count: int,
    gap_count: int,
) -> None:
    snapshot, metadata = lab.load_frozen_v16(lab.DEFAULT_INPUT_ROOT, source_key)

    # The replay API cannot receive the metadata. Comparison is subsequent,
    # but historical v16 encoded the gaps from these same groups; this proves
    # serialization integrity only.
    assert tuple(inspect.signature(lab.replay_serialized_matrix_groups).parameters) == ("snapshot",)
    replay = lab.replay_serialized_matrix_groups(snapshot)
    comparison = lab.compare_with_serialized_group_metadata(replay, metadata)

    assert len(replay.groups) == group_count
    assert len(replay.gap_rows) == gap_count
    assert comparison["status"] == "serialized-group-boundary-replay-exact"
    assert comparison["membership_exact"] is True
    assert comparison["independent_object_detection"] is False
    assert comparison["object_extraction_status"] == "not-tested"
    assert tuple(segment_id for item in replay.groups for segment_id in item.segment_ids) == snapshot.segment_ids


@pytest.mark.parametrize(
    ("source_key", "adapter_status"),
    (("000041", "rejected"), ("09", "unresolved")),
)
def test_current_stage5_adapter_fails_closed_without_pixel_axis_laundering(
    lab: ModuleType,
    source_key: str,
    adapter_status: str,
) -> None:
    snapshot, _metadata = lab.load_frozen_v16(lab.DEFAULT_INPUT_ROOT, source_key)
    mismatch = lab.current_stage5_contract_mismatch(snapshot)

    assert mismatch["status"] == "incompatible"
    assert mismatch["diagnostic_current_adapter"]["status"] == adapter_status
    assert mismatch["diagnostic_current_adapter"]["stats_provenance"] == (
        "frozen-precomputed-rejected-adapter-diagnostic"
    )
    assert mismatch["diagnostic_current_adapter"]["computed_by_this_lab"] is False
    assert mismatch["nonrectangular_legacy_payloads"]
    assert "Stage5.OverlappingBlockPlanner.plan" in mismatch["skipped"]
    with pytest.raises(lab.ProductionContractMismatch, match="pixel-axis laundering"):
        lab.require_current_stage5_compatible(snapshot)


@pytest.mark.parametrize(
    ("source_key", "sliding_four_unresolved"),
    (("000041", 3), ("09", 5)),
)
def test_sliding_windows_are_group_scoped_and_four_is_supplemental_only(
    lab: ModuleType,
    source_key: str,
    sliding_four_unresolved: int,
) -> None:
    snapshot, _metadata = lab.load_frozen_v16(lab.DEFAULT_INPUT_ROOT, source_key)
    replay = lab.replay_serialized_matrix_groups(snapshot)
    unresolved_by_strategy = {"sliding-2": 0, "sliding-4": 0}

    for serialized_group in replay.groups:
        for strategy, window_size in (("sliding-2", 2), ("sliding-4", 4)):
            blocks = lab.plan_sliding_blocks(
                snapshot,
                serialized_group,
                window_size=window_size,
                strategy=strategy,
            )
            width = min(window_size, len(serialized_group.segment_ids))
            assert len(blocks) == len(serialized_group.segment_ids) - width + 1
            assert tuple(item.segment_ids for item in blocks) == tuple(
                serialized_group.segment_ids[index : index + width]
                for index in range(len(serialized_group.segment_ids) - width + 1)
            )
            assert all(item.scope_id == serialized_group.group_id for item in blocks)
            assert all(set(item.segment_ids) <= set(serialized_group.segment_ids) for item in blocks)
            signatures = lab.topology_signatures(serialized_group.segment_ids, blocks)
            unresolved_by_strategy[strategy] += len(signatures["unresolved_groups"])

    assert unresolved_by_strategy == {
        "sliding-2": 0,
        "sliding-4": sliding_four_unresolved,
    }

    # Membership is object-local, while literal overlapping legacy source boxes
    # still make the 000041 physical-crop collision visible and explicit.
    if source_key == "000041":
        first = replay.groups[5]
        exposures = lab.foreign_bbox_exposure(
            snapshot,
            first,
            lab.plan_sliding_blocks(snapshot, first, window_size=2, strategy="sliding-2"),
        )
        assert any(
            item["segment_id"] == "leaf-000012" and item["scope_relation"] == "cross-serialized-group"
            for exposure in exposures
            for item in exposure["foreign"]
        )


def test_synthetic_decoder_uses_lattice_not_segment_bbox_and_keeps_real_pipe(
    lab: ModuleType,
) -> None:
    page, blocks, segment_ids, observations, expected = lab.synthetic_observed_word_case()
    page.close()

    assert tuple(inspect.signature(lab.decode_observed_word_lattice).parameters) == (
        "blocks",
        "source_segment_ids",
        "observations",
    )
    result = lab.decode_observed_word_lattice(
        blocks=blocks,
        source_segment_ids=segment_ids,
        observations=observations,
    )

    assert result.segment_texts == expected
    assert dict(result.segment_texts)["synthetic-segment-3"] == "|"
    assert len(result.unassigned) == 3
    assert {reason for _, reason in result.unassigned} == {
        "incomplete-or-unknown-observation-lattice",
        "observed-word-geometry-collision",
    }
    assert sum(text.count("|") for _, text in result.segment_texts) == 1


def test_observed_word_ids_are_global_and_local_boxes_must_fit_bound_crop(
    lab: ModuleType,
) -> None:
    page, blocks, segment_ids, observations, _expected = lab.synthetic_observed_word_case()
    page.close()
    first = observations[0]
    later_in_another_block = next(item for item in observations if item.block_id != first.block_id)
    duplicate_global_id = replace(
        later_in_another_block,
        observation_id=first.observation_id,
    )
    with pytest.raises(lab.LabInvariantError, match="globally unique"):
        lab.decode_observed_word_lattice(
            blocks=blocks,
            source_segment_ids=segment_ids,
            observations=observations + (duplicate_global_id,),
        )

    outside_crop = replace(
        first,
        observation_id="outside-bound-block",
        local_bbox=lab.WordBox(0, 0, 201, 1),
    )
    with pytest.raises(lab.LabInvariantError, match="outside its bound block crop"):
        lab.decode_observed_word_lattice(
            blocks=blocks,
            source_segment_ids=segment_ids,
            observations=(outside_crop,) + observations[1:],
        )


def test_ready_text_stage7_is_exact_candidate_but_never_certified(
    lab: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_assemble(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("public provenance replay must not run")

    monkeypatch.setattr(lab.DocumentAssembler, "assemble", forbidden_assemble)
    page, blocks, segment_ids, observations, _expected = lab.synthetic_observed_word_case()
    page.close()
    decoded = lab.decode_observed_word_lattice(
        blocks=blocks,
        source_segment_ids=segment_ids,
        observations=observations,
    )
    root = tmp_path / "stage7"
    summary = lab.run_ready_text_stage7(decoded.segment_texts, root)

    assert summary["status"] == "candidate-exact-production-unresolved"
    assert summary["candidate_status"] == "exact"
    assert summary["candidate_text_exact"] is True
    assert summary["candidate_markdown_exact"] is True
    assert summary["text_sha256"] == summary["expected_text_sha256"]
    assert summary["markdown_sha256"] == summary["expected_markdown_sha256"]
    assert summary["production_certification"] == "unresolved-no-ocr-provenance"
    assert summary["segment_assembly_statuses"] == ["unresolved"]
    assert summary["structural_unit_statuses"] == ["unresolved"]
    assert (root / "candidate-document.txt").read_text(encoding="utf-8") == ("item\tbeta\ngamma\t|\nitem\tzeta")
    assert "\\|" in (root / "candidate-document.md").read_text(encoding="utf-8")
    assert not (root / "document.txt").exists()
    on_disk = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert on_disk == summary


def test_ready_text_stage7_rejects_candidate_that_differs_from_explicit_oracle(
    lab: ModuleType,
    tmp_path: Path,
) -> None:
    page, blocks, segment_ids, observations, _expected = lab.synthetic_observed_word_case()
    page.close()
    decoded = lab.decode_observed_word_lattice(
        blocks=blocks,
        source_segment_ids=segment_ids,
        observations=observations,
    )
    changed = ((decoded.segment_texts[0][0], "wrong"),) + decoded.segment_texts[1:]
    with pytest.raises(lab.LabInvariantError, match="explicit TXT/MD oracle"):
        lab.run_ready_text_stage7(changed, tmp_path / "wrong-stage7")


def test_one_source_full_lab_publishes_fail_closed_contract_immutably(
    lab: ModuleType,
    tmp_path: Path,
) -> None:
    destination = lab.run_lab(
        input_root=lab.DEFAULT_INPUT_ROOT,
        output_root=tmp_path,
        run_id="focused-v16-handoff",
        source_keys=("000041",),
        manual_review_status="first-eight-inspected",
    )
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    source = manifest["sources"][0]
    strategies = {item["strategy"]: item for item in source["topology_strategies"]}

    assert manifest["status"] == "fixture-integrity-pass-production-handoff-reject"
    assert manifest["manual_review_status"] == "first-eight-inspected"
    inventory_binding = manifest["reviewed_files_inventory"]
    inventory_path = destination / inventory_binding["path"]
    assert lab._sha256_path(inventory_path) == inventory_binding["sha256"]
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory_binding["files"] == 16
    assert inventory["manual_review_status"] == "first-eight-inspected"
    assert inventory["contact_sheets_excluded"] is True
    assert inventory["sources"] == [
        {
            "source_key": "000041",
            "actual_segments": 8,
            "sliding_2_actual_blocks": 8,
        }
    ]
    assert len(inventory["files"]) == 16
    assert {item["kind"] for item in inventory["files"]} == {
        "actual-segment",
        "sliding-2-actual-block",
    }
    for item in inventory["files"]:
        reviewed_path = destination / item["path"]
        assert reviewed_path.is_file()
        assert lab._sha256_path(reviewed_path) == item["sha256"]
        assert "contact-sheet" not in item["path"]
    assert source["matrix_group_integrity"] == ("serialized-group-boundary-replay-exact")
    assert source["independent_object_extraction"] == "not-tested"
    assert source["production_handoff"] == "rejected"
    assert strategies["sliding-2"]["topology_signature_eligible"] is True
    assert strategies["sliding-2"]["physical_crop_membership_eligible"] is False
    assert strategies["sliding-2"]["sole_decoder_eligible"] is False
    assert strategies["sliding-2"]["real_ocr_status"] == "blocked-invalid-blocks"
    assert strategies["sliding-2"]["crop_membership_isolation"] == "failed"
    assert strategies["sliding-4"]["topology_signature_eligible"] is False
    assert strategies["sliding-4"]["physical_crop_membership_eligible"] is False
    assert strategies["sliding-4"]["sole_decoder_eligible"] is False
    assert strategies["sliding-4"]["role"] == ("supplemental-context-only-rejected-as-sole-decoder")
    assert manifest["ready_text_stage7"]["production_certification"] == ("unresolved-no-ocr-provenance")
    first_crop = (
        destination
        / "sources"
        / "000041"
        / "stage-a-serialized-group-integrity"
        / "actual-segments"
        / "leaf-000000.png"
    )
    assert lab._sha256_path(first_crop) == ("4209041b671a3764e94278a19793bab5fa0ed798fbccfdeefa9de28fb2a697ea")
    assert not any("overlay" in path.name.casefold() for path in destination.rglob("*"))

    with pytest.raises(FileExistsError):
        lab.run_lab(
            input_root=lab.DEFAULT_INPUT_ROOT,
            output_root=tmp_path,
            run_id="focused-v16-handoff",
            source_keys=("000041",),
        )
