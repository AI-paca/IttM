from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.sparse_pipeline import TutorialArtifactWriter as LazyTutorialWriter
from app.sparse_pipeline.block_artifacts import BlockArtifactWriter
from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlanningConfig,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import Box
from app.sparse_pipeline.crop_enhancement import (
    CropEnhancementConfig,
    CropInput,
    EnhancementBackend,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.document_artifacts import DocumentArtifactWriter
from app.sparse_pipeline.document_assembly import (
    AssemblyStatus,
    DocumentAssembler,
    DocumentAssemblyResult,
)
from app.sparse_pipeline.geometry import GeometryBundle
from app.sparse_pipeline.object_reconstruction import (
    ObjectReconstructionConfig,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_adapter_contracts import OcrOutputGeometry
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
)
from app.sparse_pipeline.ocr_artifacts import OcrArtifactWriter
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrWord,
)
from app.sparse_pipeline.pipeline_control import run_pipeline_control
from app.sparse_pipeline.pipeline_evidence import SparsePipelineEvidence
from app.sparse_pipeline.recursive_control import RunOutcome
from app.sparse_pipeline.tutorial_artifacts import TutorialArtifactWriter
from tests.sparse_pipeline.test_document_artifacts_stage7 import (
    _geometry,
    _queue,
)


@dataclass(frozen=True)
class _Fixture:
    control: RunOutcome[str]
    geometry_bundle: GeometryBundle
    evidence: SparsePipelineEvidence
    document: DocumentAssemblyResult


def _page() -> tuple[CropInput, np.ndarray, np.ndarray, np.ndarray]:
    output = io.BytesIO()
    image = Image.new("RGB", (128, 44), "white")
    try:
        draw = ImageDraw.Draw(image)
        # These two ink strips contain exactly the foreground counts recorded
        # by the shared two-segment geometry fixture: 104*3 and 108*3.
        draw.rectangle((8, 4, 111, 6), fill="black")
        draw.rectangle((8, 24, 115, 26), fill="black")
        array = np.asarray(image, dtype=np.uint8).copy()
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    foreground = np.any(array < 128, axis=2)
    ownership = np.full(foreground.shape, -1, dtype=np.int32)
    ownership[4:7, 8:112] = 0
    ownership[24:27, 8:116] = 1
    return CropInput("tutorial-page", output.getvalue()), array, foreground, ownership


def _fixture(*, unassigned: bool = False) -> _Fixture:
    segment_ids = ("segment-000000", "segment-000001")
    geometry = _geometry(segment_ids)
    page, page_rgb, foreground, ownership = _page()
    object_config = ObjectReconstructionConfig()
    objects = ObjectReconstructor(object_config).reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    planning_config = BlockPlanningConfig(
        max_core_segments=1,
        context_segments=1,
        padding=2,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    assert len(plan.blocks) == 2
    assert len(plan.adjacent_algebra) == 1
    crop_config = BlockCropConfig(
        enhancement_backend=EnhancementBackend.NUMPY,
    )
    crops, aligned_rgb_sha256 = BlockCropper(
        crop_config
    ).crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    queue = _queue(
        geometry=geometry,
        plan=plan,
        crops=crops,
        text_by_segment={
            segment_ids[0]: "Hello",
            segment_ids[1]: "Мир中文",
        },
        partial=False,
    )
    if unassigned:
        first = queue.jobs[0]
        assert first.output is not None
        words = (
            *first.output.words,
            OcrWord("Noise", Box(120, 2, 127, 6), 0.25),
        )
        jobs = (
            replace(
                first,
                output=OcrEngineOutput(
                    " ".join(item.text for item in words),
                    words,
                    OcrOutputGeometry.WORD_BOXES,
                ),
            ),
            *queue.jobs[1:],
        )
        queue = replace(queue, jobs=jobs)

    fusion_config = OcrFusionConfig()
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    stage4_config = CropEnhancementConfig(
        backend=EnhancementBackend.NUMPY,
    )
    # Deliberately use a stage-local ID.  The tutorial writer must preserve
    # page bytes while safely normalizing this ID for the stage writer.
    stage4 = GammaDarkCropEnhancer(stage4_config).enhance(
        CropInput("tutorial-stage4-page", page.png_bytes)
    )
    evidence = SparsePipelineEvidence(
        page=page,
        geometry=geometry,
        objects=objects,
        plan=plan,
        crops=crops,
        queue=queue,
        fusion=fusion,
        stage4=stage4,
        object_config=object_config,
        planning_config=planning_config,
        crop_config=crop_config,
        fusion_config=fusion_config,
        stage4_config=stage4_config,
    )
    document = DocumentAssembler().assemble(
        page=page,
        geometry=geometry,
        objects=objects,
        plan=plan,
        crops=crops,
        queue=queue,
        fusion=fusion,
        object_config=object_config,
        planning_config=planning_config,
        crop_config=crop_config,
        fusion_config=fusion_config,
    )
    geometry_bundle = GeometryBundle(
        result=geometry,
        source_rgb=np.array(page_rgb, copy=True),
        aligned_rgb=np.array(page_rgb, copy=True),
        foreground_mask=np.array(foreground, copy=True),
        rule_mask=np.zeros(foreground.shape, dtype=np.bool_),
        ownership=np.array(ownership, copy=True),
    )
    return _Fixture(
        control=run_pipeline_control(),
        geometry_bundle=geometry_bundle,
        evidence=evidence,
        document=document,
    )


def _publish(root: Path, fixture: _Fixture, *, run_id: str) -> Path:
    return TutorialArtifactWriter().write(
        root,
        run_id=run_id,
        control=fixture.control,
        geometry=fixture.geometry_bundle,
        evidence=fixture.evidence,
        document=fixture.document,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def test_publishes_all_stages_and_tutorial_visuals_atomically(
    tmp_path: Path,
) -> None:
    fixture = _fixture()

    published = _publish(tmp_path, fixture, run_id="tutorial-complete")

    assert LazyTutorialWriter is TutorialArtifactWriter
    manifest = json.loads((published / "manifest.json").read_text("utf-8"))
    assert manifest["execution_order"] == [3, 1, 6, 4, 5, 2, 7]
    assert manifest["status"] == "COMPLETE"
    assert manifest["status_vocabulary"] == [
        "COMPLETE",
        "UNRESOLVED",
        "FAILED",
    ]
    assert [item["directory"] for item in manifest["stages"]] == [
        "03-control",
        "01-geometry",
        "06-objects",
        "04-enhancement",
        "05-blocks",
        "02-ocr",
        "07-document",
    ]
    assert [item["status"] for item in manifest["stages"]] == [
        "COMPLETE"
    ] * 7
    expected_visuals = (
        "04-enhancement/abs-diff.png",
        "04-enhancement/source/tutorial-stage4-page.png",
        "04-enhancement/output/tutorial-stage4-page.png",
        "04-enhancement/candidate-gallery.md",
        "04-enhancement/production-block-manifest.json",
        "04-enhancement/production-block-gallery.md",
        "01-geometry/segment-crops/raw/segment-000000.png",
        "01-geometry/segment-crops/isolated/segment-000000.png",
        "01-geometry/segment-crops/contact-sheets/segments-000000-000001.png",
        "01-geometry/segment-crops/gallery.md",
        "05-blocks/page-block-overlay.png",
        "05-blocks/crop-gallery.md",
        "05-blocks/membership-gallery.md",
        "05-blocks/contact-sheets/blocks-000000-000001.png",
        "05-blocks/adjacent-pairs/pair-000000/intersection-ink-mask.png",
        "05-blocks/adjacent-pairs/pair-000000/union-bbox-mask.png",
        "05-blocks/adjacent-pairs/pair-000000/xor-ink-mask.png",
        "02-ocr/word-box-overlay.png",
    )
    assert all((published / item).is_file() for item in expected_visuals)
    pair_manifest = json.loads(
        (
            published
            / "05-blocks/adjacent-pairs/pair-000000/manifest.json"
        ).read_text("utf-8")
    )
    assert pair_manifest["mask_basis"] == {
        "bbox": "filled half-open stage-1 segment bounding boxes",
        "ink_region": "stage-1 ownership intersected with foreground-mask",
    }
    assert pair_manifest["visuals_rendered"] is True
    block_visuals = json.loads(
        (published / "05-blocks/tutorial-visuals.json").read_text("utf-8")
    )
    assert block_visuals["adjacent_pair_count"] == 1
    assert block_visuals["rendered_adjacent_pair_count"] == 1
    assert block_visuals["visual_pair_limit"] == 8
    segment_crops = json.loads(
        (
            published / "01-geometry/segment-crops/manifest.json"
        ).read_text("utf-8")
    )
    assert segment_crops["segments"] == 2
    assert [item["segment_id"] for item in segment_crops["items"]] == [
        "segment-000000",
        "segment-000001",
    ]
    assert all(
        item["ownership_pixels"] == item["ink_pixels"]
        for item in segment_crops["items"]
    )
    isolated = np.asarray(
        Image.open(
            published
            / "01-geometry/segment-crops/isolated/segment-000000.png"
        ).convert("RGB")
    )
    assert isolated.shape == (12, 104, 3)
    assert int(np.count_nonzero(np.any(isolated != 255, axis=2))) == 312
    block_crops = json.loads(
        (published / "05-blocks/crop-gallery.json").read_text("utf-8")
    )
    assert block_crops["blocks"] == 2
    assert block_crops["items"][0]["core_segment_ids"] == [
        "segment-000000"
    ]
    assert block_crops["items"][0]["context_segment_ids"] == []
    assert block_crops["items"][0]["segment_ids"] == ["segment-000000"]
    assert block_crops["stage4_calibration"] == {
        "backend": "numpy",
        "manifest": "../04-enhancement/manifest.json",
        "recipe": "kornia-gamma-dark-v1",
        "scope": "full aligned page",
    }
    assert block_crops["stage5_application"]["scope"] == (
        "each raw block independently"
    )
    assert block_crops["invariants"] == {
        "gamma_backend_matches_stage4": True,
        "gamma_geometry_unchanged": True,
        "gamma_recipe_matches_stage4": True,
        "gamma_recomputed_per_raw_block": True,
    }
    assert all(
        item["gamma_recipe_matches_stage4"]
        and item["gamma_backend_matches_stage4"]
        and item["gamma_geometry_unchanged"]
        for item in block_crops["items"]
    )
    stage4_gallery = (
        published / "04-enhancement/candidate-gallery.md"
    ).read_text("utf-8")
    assert "GLOBAL CALIBRATION ONLY — NOT OCR INPUT" in stage4_gallery
    assert "abs-diff" not in stage4_gallery
    production = json.loads(
        (
            published / "04-enhancement/production-block-manifest.json"
        ).read_text("utf-8")
    )
    assert production["scope"] == "each Stage 5 raw block independently"
    assert production["global_calibration"]["not_ocr_input"] is True
    assert production["invariants"] == {
        "full_page_output_not_used_as_ocr_input": True,
        "per_block_output_is_recomputed_from_source": True,
        "per_block_source_is_stage5_raw_crop": True,
        "recipe_backend_match_global_calibration": True,
        "same_geometry": True,
    }
    tutorial = (published / "tutorial.md").read_text("utf-8")
    assert "3 → 1 → 6 → 4 → 5 → 2 → 7" in tutorial
    assert "(01-geometry/segment-crops/gallery.md)" in tutorial
    assert "(04-enhancement/production-block-gallery.md)" in tutorial
    assert "(05-blocks/membership-gallery.md)" in tutorial
    assert "(02-ocr/word-box-overlay.png)" in tutorial
    assert (published / "sparse-matrix.json").is_file()
    assert (published / "sparse-matrix.tsv").is_file()
    assert (published / "sparse-matrix.png").is_file()
    assert (published / "sparse-matrix-ownership.png").read_bytes() == (
        published / "01-geometry/ownership.png"
    ).read_bytes()
    assert (published / "document.md").read_text("utf-8") == (
        published / "07-document/certified/document.md"
    ).read_text("utf-8")
    assert len(tuple((published / "logs").glob("*.log"))) == 7
    assert "2\t1\tgeometry-sparse-matrix" in (
        published / "logs/stages.tsv"
    ).read_text("utf-8")
    provenance = json.loads((published / "provenance.json").read_text("utf-8"))
    assert provenance["execution_order"] == [3, 1, 6, 4, 5, 2, 7]
    assert provenance["provided"] == {}

    objects = json.loads(
        (published / "objects/manifest.json").read_text("utf-8")
    )
    assert objects["literal_crops_only"] is True
    assert objects["overlay_is_evidence"] is False
    assert objects["objects"]
    for object_entry in objects["objects"]:
        object_root = published / "objects" / object_entry["object_id"]
        assert (object_root / "object.md").is_file()
        assert (object_root / "object.txt").is_file()
        assert (object_root / "debag.md").is_file()
        object_record = json.loads((object_root / "object.json").read_text("utf-8"))
        for segment_id in object_record["segment_ids"]:
            assert (object_root / "segments" / f"{segment_id}.txt").is_file()
            assert (
                object_root / "segments" / f"{segment_id}.png"
            ).read_bytes() == (
                published
                / "01-geometry/segment-crops/raw"
                / f"{segment_id}.png"
            ).read_bytes()
            assert (
                object_root / "segments" / f"{segment_id}.isolated.png"
            ).read_bytes() == (
                published
                / "01-geometry/segment-crops/isolated"
                / f"{segment_id}.png"
            ).read_bytes()
        for block_id in object_record["block_ids"]:
            block_root = object_root / "blocks" / block_id
            assert (
                block_root / "raw.png"
            ).read_bytes() == (
                published / "05-blocks/raw" / f"{block_id}.png"
            ).read_bytes()
            assert (
                block_root / "enhanced.png"
            ).read_bytes() == (
                published / "05-blocks/gamma" / f"{block_id}.png"
            ).read_bytes()
            membership = json.loads(
                (block_root / "membership.json").read_text("utf-8")
            )
            isolation = json.loads(
                (block_root / "isolation.json").read_text("utf-8")
            )
            assert membership["isolation"] == "isolation.json"
            assert membership["isolation_mask"] is None
            assert membership["masked_segment_ids"] == []
            assert isolation["applied"] is False
            assert isolation["mask_png"] is None
            assert isolation["masked_segment_ids"] == []
            assert isolation["provenance"]["source_stage"] == 1
            assert len(
                isolation["provenance"]["ownership_raster_sha256"]
            ) == 64
            assert (block_root / "ocr.json").is_file()
    assert not tuple(tmp_path.glob(".tutorial-complete.partial-*"))


def test_block_isolation_artifact_persists_exact_mask_and_provenance(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    block = fixture.evidence.plan.blocks[0]
    original = fixture.evidence.crops[0]
    with Image.open(io.BytesIO(original.raw.png_bytes)) as opened:
        raw = np.asarray(opened.convert("RGB"), dtype=np.uint8)
    background = np.argwhere(np.all(raw == 255, axis=2))
    assert len(background) > 0
    row, column = (int(item) for item in background[0])
    mask = Image.new("L", (block.bbox.width, block.bbox.height), 0)
    output = io.BytesIO()
    try:
        mask.putpixel((column, row), 255)
        mask.save(output, format="PNG")
    finally:
        mask.close()
    mask_png = output.getvalue()
    foreign_id = "segment-000001"
    crop = replace(
        original,
        isolation_mask_png=mask_png,
        masked_segment_ids=(foreign_id,),
    )
    target = tmp_path / "object-000000" / "blocks" / block.block_id
    target.mkdir(parents=True)

    result = TutorialArtifactWriter._write_block_isolation_evidence(
        target,
        crop=crop,
        block=block,
        object_id="object-000000",
        owner_by_segment={
            block.segment_ids[0]: "object-000000",
            foreign_id: "object-000001",
        },
        ownership_sha256="a" * 64,
        ownership_order=(block.segment_ids[0], foreign_id),
    )

    assert result == "isolation-mask.png"
    assert (target / result).read_bytes() == mask_png
    record = json.loads((target / "isolation.json").read_text("utf-8"))
    assert record["applied"] is True
    assert record["mask_png"] == result
    assert record["mask_sha256"] == hashlib.sha256(mask_png).hexdigest()
    assert record["masked_segment_ids"] == [foreign_id]
    assert record["masked_segment_owners"] == {
        foreign_id: "object-000001"
    }
    assert record["provenance"] == {
        "source_stage": 1,
        "source": "exact Stage 1 ownership raster",
        "ownership_raster_sha256": "a" * 64,
        "ownership_segment_ids": [block.segment_ids[0], foreign_id],
        "validation": (
            "mask and masked IDs were recomputed from Stage 1 ownership and "
            "checked before artifact publication"
        ),
        "declared_members_preserved": True,
        "masked_segments_are_foreign_objects": True,
    }


def test_publication_passes_exact_geometry_matrix_to_block_and_ocr_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    exact_matrix = fixture.evidence.geometry.matrix
    original_block_write = BlockArtifactWriter._write_stage
    original_ocr_write = OcrArtifactWriter._write_stage
    observed: list[tuple[str, object]] = []

    def write_blocks(stage: Path, **kwargs: object) -> None:
        observed.append(("blocks", kwargs.get("matrix")))
        original_block_write(stage, **kwargs)  # type: ignore[arg-type]

    def write_ocr(stage: Path, **kwargs: object) -> None:
        observed.append(("ocr", kwargs.get("matrix")))
        original_ocr_write(stage, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(BlockArtifactWriter, "_write_stage", write_blocks)
    monkeypatch.setattr(OcrArtifactWriter, "_write_stage", write_ocr)

    published = _publish(tmp_path, fixture, run_id="exact-matrix-handoff")

    assert published.is_dir()
    assert observed == [("blocks", exact_matrix), ("ocr", exact_matrix)]


def test_pair_manifest_survives_when_raster_visual_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    monkeypatch.setattr(TutorialArtifactWriter, "_MAX_RENDERED_PAIR_VISUALS", 0)

    published = _publish(tmp_path, fixture, run_id="tutorial-manifest-only")

    pair = published / "05-blocks/adjacent-pairs/pair-000000"
    manifest = json.loads((pair / "manifest.json").read_text("utf-8"))
    assert manifest["visuals_rendered"] is False
    assert manifest["files"] == {}
    assert not tuple(pair.glob("*-mask.png"))
    block_visuals = json.loads(
        (published / "05-blocks/tutorial-visuals.json").read_text("utf-8")
    )
    assert block_visuals["adjacent_pair_count"] == 1
    assert block_visuals["rendered_adjacent_pair_count"] == 0
    assert block_visuals["visual_pair_limit"] == 0


def test_unresolved_ocr_and_document_are_not_reported_complete(
    tmp_path: Path,
) -> None:
    fixture = _fixture(unassigned=True)
    assert fixture.document.status is AssemblyStatus.UNRESOLVED

    published = _publish(tmp_path, fixture, run_id="tutorial-unresolved")

    manifest = json.loads((published / "manifest.json").read_text("utf-8"))
    assert len(manifest["stages"]) == 7
    assert manifest["stages"][5]["semantic_stage"] == 2
    assert manifest["stages"][5]["status"] == "UNRESOLVED"
    assert manifest["stages"][6]["semantic_stage"] == 7
    assert manifest["stages"][6]["status"] == "UNRESOLVED"
    assert manifest["status"] == "UNRESOLVED"
    overlay = np.asarray(
        Image.open(published / "02-ocr/word-box-overlay.png").convert("RGB")
    )
    assert np.any(np.all(overlay == (220, 35, 45), axis=2))


def test_rejects_missing_stage4_and_reordered_control_before_publication(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    missing_stage4 = replace(fixture.evidence, stage4=None)
    with pytest.raises(ValueError, match="requires the Stage 4"):
        TutorialArtifactWriter().write(
            tmp_path,
            run_id="missing-stage4",
            control=fixture.control,
            geometry=fixture.geometry_bundle,
            evidence=missing_stage4,
            document=fixture.document,
        )
    reordered_control = replace(
        fixture.control,
        evidence=tuple(reversed(fixture.control.evidence)),
    )
    with pytest.raises(ValueError, match="reordered"):
        TutorialArtifactWriter().write(
            tmp_path,
            run_id="bad-control",
            control=reordered_control,
            geometry=fixture.geometry_bundle,
            evidence=fixture.evidence,
            document=fixture.document,
        )
    assert not (tmp_path / "missing-stage4").exists()
    assert not (tmp_path / "bad-control").exists()


def test_rejects_foreign_geometry_document_and_existing_destination(
    tmp_path: Path,
) -> None:
    complete = _fixture()
    unresolved = _fixture(unassigned=True)
    foreign_geometry = replace(
        complete.geometry_bundle,
        result=replace(
            complete.evidence.geometry,
            diagnostics=("foreign-run",),
        ),
    )
    with pytest.raises(ValueError, match="bundle and sparse pipeline"):
        TutorialArtifactWriter().write(
            tmp_path,
            run_id="foreign-geometry",
            control=complete.control,
            geometry=foreign_geometry,
            evidence=complete.evidence,
            document=complete.document,
        )
    with pytest.raises(ValueError, match="was not assembled"):
        TutorialArtifactWriter().write(
            tmp_path,
            run_id="foreign-document",
            control=complete.control,
            geometry=complete.geometry_bundle,
            evidence=complete.evidence,
            document=unresolved.document,
        )

    published = _publish(tmp_path, complete, run_id="immutable")
    before = _tree_bytes(published)
    with pytest.raises(FileExistsError):
        _publish(tmp_path, complete, run_id="immutable")
    assert _tree_bytes(published) == before


def test_writer_failure_removes_the_whole_partial_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fixture publication failure")

    monkeypatch.setattr(DocumentArtifactWriter, "_write_stage", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        _publish(tmp_path, fixture, run_id="transaction-failure")

    assert not (tmp_path / "transaction-failure").exists()
    assert not tuple(tmp_path.glob(".transaction-failure.partial-*"))
