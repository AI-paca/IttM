"""Adversarial audit cases for Stage 7.

These tests intentionally describe fail-closed behavior.  A failure is a
concrete regression proof for the production audit; this module does not
modify Stage 7 implementation code.
"""

from pathlib import Path

import pytest

from app.sparse_pipeline.atomic_publish import rename_no_replace
from app.sparse_pipeline.document_artifacts import DocumentArtifactWriter
from app.sparse_pipeline.block_planning import OverlappingBlockPlanner
from app.sparse_pipeline.contracts import Box, RuleAxis
from app.sparse_pipeline.document_assembly import AssemblyStatus
from app.sparse_pipeline.object_reconstruction import ObjectKind, ObjectReconstructor
from app.sparse_pipeline.ocr_fusion import OcrEvidenceFusion, OcrFusionStatus

from tests.sparse_pipeline.test_document_assembly_stage7 import (
    _Fixture,
    _assemble,
    _crops,
    _fixture,
    _geometry,
    _matrix,
    _paragraph_geometry,
    _queue_lanes,
    _rule,
    _segment,
    _text_only,
    _two_object_overlap_fixture,
    _word_boxes,
)
from tests.sparse_pipeline.test_document_artifacts_stage7 import (
    _bundle as _artifact_bundle,
    _publish as _publish_artifacts,
)


def test_stable_object_text_cannot_override_conflicting_low_confidence_segments() -> None:
    """Selected-but-unresolved segment codepoints remain contradictory evidence."""

    geometry = _paragraph_geometry()
    objects = ObjectReconstructor().reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner().plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page, crops = _crops(geometry, plan)
    queue = _queue_lanes(
        plan,
        crops,
        {
            "text-lane": _text_only({"block-000000": "OBJECT TEXT"}),
            "bbox-lane": _word_boxes(
                geometry,
                {"p-0": "CONTRADICTING", "p-1": "GLYPHS"},
                confidence=0.1,
            ),
        },
    )
    fusion = OcrEvidenceFusion().fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    assert fusion.status is OcrFusionStatus.UNRESOLVED
    assert all(item.selected_text for item in fusion.segments)
    assert all(item.unresolved for item in fusion.segments)

    result = _assemble(_Fixture(geometry, objects, plan, crops, queue, fusion, page))

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[0].text is None


def test_xor_subtraction_requires_an_observed_object_boundary() -> None:
    """A suffix match inside one token is not a unique structural anchor."""

    result = _assemble(_two_object_overlap_fixture(anchor_text="CAT", source_text="SCAT"))

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].text is None


def test_xor_subtraction_respects_stage6_reading_direction() -> None:
    """Context preceding a core object cannot be certified in reverse order."""

    result = _assemble(
        _two_object_overlap_fixture(
            anchor_text="FIRST",
            source_text="SECOND FIRST",
        )
    )

    assert result.status is AssemblyStatus.UNRESOLVED
    assert result.text is None
    assert result.objects[1].text is None


def test_table_keeps_a_fully_empty_leading_logical_column() -> None:
    """The enclosing rule grid, not occupied segment extrema, defines table axes."""

    width, height = 210, 46
    geometry = _geometry(
        aligned_size=(width, height),
        segments=(
            _segment("x-01", Box(64, 4, 126, 20), row_index=0, component_id=0),
            _segment("x-02", Box(134, 4, 204, 20), row_index=0, component_id=1),
            _segment("x-11", Box(64, 26, 126, 42), row_index=1, component_id=2),
            _segment("x-12", Box(134, 26, 204, 42), row_index=1, component_id=3),
        ),
        rules=(
            _rule("x-h0", Box(0, 0, width, 2), axis=RuleAxis.HORIZONTAL),
            _rule("x-h1", Box(0, 22, width, 24), axis=RuleAxis.HORIZONTAL),
            _rule("x-h2", Box(0, 44, width, 46), axis=RuleAxis.HORIZONTAL),
            _rule("x-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
            _rule("x-v1", Box(60, 0, 62, height), axis=RuleAxis.VERTICAL),
            _rule("x-v2", Box(130, 0, 132, height), axis=RuleAxis.VERTICAL),
            _rule("x-v3", Box(208, 0, 210, height), axis=RuleAxis.VERTICAL),
        ),
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 60, 62, 130, 132, 208, 210),
            placements={
                "x-01": ((1, 3),),
                "x-02": ((1, 5),),
                "x-11": ((3, 3),),
                "x-12": ((3, 5),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4, 6),
        ),
    )
    texts = {"x-01": "A", "x-02": "B", "x-11": "C", "x-12": "D"}
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert fixture.objects.objects[0].kind is ObjectKind.TABLE

    result = _assemble(fixture)

    assert result.objects[0].table_column_indices == (1, 3, 5)
    assert len(result.structural_units) == 6
    assert result.candidate_markdown.splitlines() == [
        "|  | A | B |",
        "|  | C | D |",
    ]


def test_table_span_uses_perpendicular_border_bands_not_rule_overhangs() -> None:
    """A horizontal-rule overhang must not become an invented table column."""

    width, height = 220, 46
    geometry = _geometry(
        aligned_size=(width, height),
        segments=(
            _segment("o-00", Box(4, 4, 56, 20), row_index=0, component_id=0),
            _segment("o-01", Box(64, 4, 126, 20), row_index=0, component_id=1),
            _segment("o-10", Box(4, 26, 56, 42), row_index=1, component_id=2),
            _segment("o-11", Box(64, 26, 126, 42), row_index=1, component_id=3),
        ),
        rules=(
            # Horizontal strokes overhang the actual right vertical border.
            _rule("o-h0", Box(0, 0, 210, 2), axis=RuleAxis.HORIZONTAL),
            _rule("o-h1", Box(0, 22, 210, 24), axis=RuleAxis.HORIZONTAL),
            _rule("o-h2", Box(0, 44, 210, 46), axis=RuleAxis.HORIZONTAL),
            _rule("o-v0", Box(0, 0, 2, height), axis=RuleAxis.VERTICAL),
            _rule("o-v1", Box(60, 0, 62, height), axis=RuleAxis.VERTICAL),
            _rule("o-v2", Box(130, 0, 132, height), axis=RuleAxis.VERTICAL),
        ),
        matrix=_matrix(
            row_edges=(0, 2, 22, 24, 44, 46),
            column_edges=(0, 2, 60, 62, 130, 132, 210, 220),
            placements={
                "o-00": ((1, 1),),
                "o-01": ((1, 3),),
                "o-10": ((3, 1),),
                "o-11": ((3, 3),),
            },
            horizontal_rule_rows=(0, 2, 4),
            vertical_rule_columns=(0, 2, 4),
        ),
    )
    texts = {"o-00": "A", "o-01": "B", "o-10": "C", "o-11": "D"}
    fixture = _fixture(geometry, _word_boxes(geometry, texts))
    assert fixture.objects.objects[0].kind is ObjectKind.TABLE

    result = _assemble(fixture)

    assert result.objects[0].table_column_indices == (1, 3)
    assert len(result.structural_units) == 4
    assert result.candidate_markdown.splitlines() == ["| A | B |", "| C | D |"]


def test_artifact_writer_never_replaces_a_dangling_destination_symlink(
    tmp_path: Path,
) -> None:
    """Path.exists() is false for a dangling symlink, but the path is occupied."""

    run = tmp_path / "claimed-run"
    link_target = tmp_path / "missing-target"
    run.symlink_to(link_target, target_is_directory=True)
    assert run.is_symlink() and not run.exists()

    with pytest.raises(OSError):
        _publish_artifacts(
            run,
            _artifact_bundle(),
            writer=DocumentArtifactWriter(),
        )

    assert run.is_symlink()
    assert run.readlink() == link_target


def test_artifact_writer_does_not_replace_empty_directory_claimed_mid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publication primitive must close the exists/rename TOCTOU window."""

    bundle = _artifact_bundle()
    run = tmp_path / "claimed-during-write"
    original = DocumentArtifactWriter._write_stage

    def write_then_claim(stage_dir: Path, result: object) -> None:
        original(stage_dir, result)  # type: ignore[arg-type]
        run.mkdir()

    monkeypatch.setattr(
        DocumentArtifactWriter,
        "_write_stage",
        staticmethod(write_then_claim),
    )

    with pytest.raises(FileExistsError):
        _publish_artifacts(
            run,
            bundle,
            writer=DocumentArtifactWriter(),
        )

    assert run.is_dir()
    assert not tuple(run.iterdir())
    assert not tuple(tmp_path.glob(".claimed-during-write.partial-*"))


def test_runner_final_publication_primitive_refuses_an_empty_destination(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".v20.partial-fixture"
    staging.mkdir()
    (staging / "summary.json").write_text("{}\n", encoding="utf-8")
    final = tmp_path / "v20"
    final.mkdir()

    with pytest.raises(FileExistsError):
        rename_no_replace(staging, final)

    assert (staging / "summary.json").is_file()
    assert final.is_dir() and not tuple(final.iterdir())
    runner_source = (
        Path(__file__).resolve().parents[3] / "scripts" / "debug" / "debug_document_assembly.py"
    ).read_text(encoding="utf-8")
    assert runner_source.count("rename_no_replace(staging, final_dir)") == 2
