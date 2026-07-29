from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
import pytest
from PIL import Image

from app.sparse_pipeline.block_crops import (
    BlockCropConfig,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (
    BlockPlan,
    BlockPlanningConfig,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import (
    AffineTransform,
    AlignmentTrace,
    AxisInterval,
    Box,
    GeometryResult,
    RecursiveNode,
    Segment,
    SegmentationResult,
    SegmentKind,
    SegmentSpan,
    SparseCell,
    SparseSegmentMatrix,
    StopReason,
)
from app.sparse_pipeline.crop_enhancement import (
    CropInput,
    EnhancementBackend,
)
from app.sparse_pipeline.document_artifacts import DocumentArtifactWriter
from app.sparse_pipeline.document_assembly import (
    AssemblyStatus,
    DocumentAssembler,
    DocumentAssemblyConfig,
    DocumentAssemblyResult,
)
from app.sparse_pipeline.object_reconstruction import (
    ObjectReconstructionConfig,
    ObjectReconstructionResult,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrFailureCode,
    OcrOutputGeometry,
    OcrResource,
)
from app.sparse_pipeline.ocr_fusion import (
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionResult,
)
from app.sparse_pipeline.ocr_queue import (
    OcrEngineOutput,
    OcrJobResult,
    OcrJobStatus,
    OcrQueueResult,
    OcrQueueStatus,
    OcrTransform,
    OcrWord,
)
from tests.sparse_pipeline.test_document_assembly_stage7 import (
    _assemble as _assemble_fallback,
    _fixture as _fallback_fixture,
    _text_only,
    _two_object_geometry,
)


@dataclass(frozen=True)
class _ArtifactBundle:
    page: CropInput
    geometry: GeometryResult
    objects: ObjectReconstructionResult
    plan: BlockPlan
    crops: tuple[BlockCropPair, ...]
    queue: OcrQueueResult
    fusion: OcrFusionResult
    result: DocumentAssemblyResult
    assembly_config: DocumentAssemblyConfig
    object_config: ObjectReconstructionConfig
    planning_config: BlockPlanningConfig
    crop_config: BlockCropConfig
    fusion_config: OcrFusionConfig


def _geometry(segment_ids: tuple[str, str]) -> GeometryResult:
    segments = (
        Segment(
            segment_id=segment_ids[0],
            bbox=Box(8, 4, 112, 16),
            source_bbox=Box(8, 4, 112, 16),
            kind=SegmentKind.TEXT,
            ink_pixels=312,
            row_index=0,
            order_key=(4, 8),
            parent_path=("geo-root",),
            component_ids=(0,),
        ),
        Segment(
            segment_id=segment_ids[1],
            bbox=Box(8, 24, 116, 36),
            source_bbox=Box(8, 24, 116, 36),
            kind=SegmentKind.TEXT,
            ink_pixels=324,
            row_index=1,
            order_key=(24, 8),
            parent_path=("geo-root",),
            component_ids=(1,),
        ),
    )
    rows = (
        AxisInterval(0, 0, 20),
        AxisInterval(1, 20, 40),
        AxisInterval(2, 40, 44),
    )
    columns = (AxisInterval(0, 0, 128),)
    matrix = SparseSegmentMatrix(
        rows=rows,
        columns=columns,
        cells=(
            SparseCell(0, 0, segment_ids[0]),
            SparseCell(1, 0, segment_ids[1]),
        ),
        spans=(
            SegmentSpan(segment_ids[0], 0, 1, 0, 1),
            SegmentSpan(segment_ids[1], 1, 2, 0, 1),
        ),
    )
    foreground_pixels = sum(item.ink_pixels for item in segments)
    return GeometryResult(
        alignment=AlignmentTrace(
            transform=AffineTransform.identity((128, 44)),
            correction_degrees=0.0,
            background_rgb=(255, 255, 255),
            content_bbox=Box.union(tuple(item.bbox for item in segments)),
            foreground_pixels=foreground_pixels,
        ),
        segmentation=SegmentationResult(
            segments=segments,
            rules=(),
            nodes=(
                RecursiveNode(
                    node_id="geo-root",
                    bbox=Box(0, 0, 128, 44),
                    depth=0,
                    parent_id=None,
                    segment_ids=segment_ids,
                    stop_reason=StopReason.ATOMIC,
                ),
            ),
            root_node_id="geo-root",
            aligned_size=(128, 44),
            foreground_pixels=foreground_pixels,
        ),
        matrix=matrix,
        aligned_rgb_sha256="0" * 64,
    )


def _page_png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (128, 44), "white")
    try:
        image.save(output, format="PNG", compress_level=1)
    finally:
        image.close()
    return output.getvalue()


def _complete_output(
    block: object,
    geometry: GeometryResult,
    text_by_segment: dict[str, str],
) -> OcrEngineOutput:
    segment_by_id = {
        item.segment_id: item for item in geometry.segmentation.segments
    }
    bbox = getattr(block, "bbox")
    words = tuple(
        OcrWord(
            text=text_by_segment[segment_id],
            bbox=Box(
                segment_by_id[segment_id].bbox.left - bbox.left,
                segment_by_id[segment_id].bbox.top - bbox.top,
                segment_by_id[segment_id].bbox.right - bbox.left,
                segment_by_id[segment_id].bbox.bottom - bbox.top,
            ),
            confidence=0.99,
        )
        for segment_id in getattr(block, "segment_ids")
    )
    return OcrEngineOutput(
        text=" ".join(item.text for item in words),
        words=words,
        geometry=OcrOutputGeometry.WORD_BOXES,
    )


def _queue(
    *,
    geometry: GeometryResult,
    plan: BlockPlan,
    crops: tuple[BlockCropPair, ...],
    text_by_segment: dict[str, str],
    partial: bool,
) -> OcrQueueResult:
    crop_by_id = {item.block_id: item for item in crops}
    jobs: list[OcrJobResult] = []
    for block in plan.blocks:
        crop = crop_by_id[block.block_id]
        context_sha256 = hashlib.sha256(crop.raw.png_bytes).hexdigest()
        for transform in (OcrTransform.RAW, OcrTransform.GAMMA):
            payload = (
                crop.raw.png_bytes
                if transform is OcrTransform.RAW
                else crop.gamma.png_bytes
            )
            common = {
                "job_id": f"ocr-job-{len(jobs):08d}",
                "block_id": block.block_id,
                "transform": transform,
                "lane_id": "artifact-fixture-lane",
                "resource": OcrResource.CPU,
                "elapsed_seconds": 0.01,
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "context_sha256": context_sha256,
                "capability_id": "artifact-fixture-capability",
            }
            if partial and transform is OcrTransform.GAMMA:
                jobs.append(
                    OcrJobResult(
                        **common,
                        status=OcrJobStatus.FAILED,
                        output=None,
                        error_type="FixtureEngineError",
                        error_message="gamma capability failed safely",
                        failure_code=OcrFailureCode.ENGINE_ERROR,
                    )
                )
            else:
                jobs.append(
                    OcrJobResult(
                        **common,
                        status=OcrJobStatus.COMPLETE,
                        output=_complete_output(
                            block,
                            geometry,
                            text_by_segment,
                        ),
                        error_type=None,
                        error_message=None,
                    )
                )
    failed = sum(item.status is OcrJobStatus.FAILED for item in jobs)
    return OcrQueueResult(
        jobs=tuple(jobs),
        status=(
            OcrQueueStatus.PARTIAL if failed else OcrQueueStatus.COMPLETE
        ),
        complete=len(jobs) - failed,
        failed=failed,
        diagnostics=("real-partial-queue",) if failed else (),
    )


def _bundle(
    *,
    partial: bool = False,
    segment_ids: tuple[str, str] = ("segment-000000", "segment-000001"),
) -> _ArtifactBundle:
    assembly_config = DocumentAssemblyConfig()
    object_config = ObjectReconstructionConfig()
    planning_config = BlockPlanningConfig(padding=2)
    crop_config = BlockCropConfig(
        enhancement_backend=EnhancementBackend.NUMPY
    )
    fusion_config = OcrFusionConfig()
    geometry = _geometry(segment_ids)
    objects = ObjectReconstructor(object_config).reconstruct(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        rules=geometry.segmentation.rules,
        matrix=geometry.matrix,
    )
    plan = OverlappingBlockPlanner(planning_config).plan(
        aligned_size=geometry.segmentation.aligned_size,
        segments=geometry.segmentation.segments,
        objects_result=objects,
    )
    page = CropInput("stage7-artifact-page", _page_png())
    crops, aligned_rgb_sha256 = BlockCropper(
        crop_config
    ).crop_with_rgb_sha256(
        page,
        aligned_size=geometry.segmentation.aligned_size,
        plan=plan,
    )
    geometry = replace(geometry, aligned_rgb_sha256=aligned_rgb_sha256)
    text_by_segment = {
        segment_ids[0]: "Hello",
        segment_ids[1]: "Мир中文",
    }
    queue = _queue(
        geometry=geometry,
        plan=plan,
        crops=crops,
        text_by_segment=text_by_segment,
        partial=partial,
    )
    fusion = OcrEvidenceFusion(fusion_config).fuse(
        plan=plan,
        segments=geometry.segmentation.segments,
        crops=crops,
        queue=queue,
    )
    assemble_kwargs = {
        "page": page,
        "geometry": geometry,
        "objects": objects,
        "plan": plan,
        "crops": crops,
        "queue": queue,
        "fusion": fusion,
        "object_config": object_config,
        "planning_config": planning_config,
        "crop_config": crop_config,
        "fusion_config": fusion_config,
    }
    result = DocumentAssembler(assembly_config).assemble(**assemble_kwargs)
    expected_status = (
        AssemblyStatus.UNRESOLVED if partial else AssemblyStatus.COMPLETE
    )
    assert result.status is expected_status
    return _ArtifactBundle(
        page=page,
        geometry=geometry,
        objects=objects,
        plan=plan,
        crops=crops,
        queue=queue,
        fusion=fusion,
        result=result,
        assembly_config=assembly_config,
        object_config=object_config,
        planning_config=planning_config,
        crop_config=crop_config,
        fusion_config=fusion_config,
    )


def _writer_kwargs(bundle: _ArtifactBundle) -> dict[str, object]:
    return {
        "page": bundle.page,
        "geometry": bundle.geometry,
        "objects": bundle.objects,
        "plan": bundle.plan,
        "crops": bundle.crops,
        "queue": bundle.queue,
        "fusion": bundle.fusion,
        "assembly_config": bundle.assembly_config,
        "object_config": bundle.object_config,
        "planning_config": bundle.planning_config,
        "crop_config": bundle.crop_config,
        "fusion_config": bundle.fusion_config,
    }


def _publish(
    root: Path,
    bundle: _ArtifactBundle,
    *,
    result: object | None = None,
    writer: DocumentArtifactWriter | None = None,
) -> Path:
    selected = bundle.result if result is None else result
    return (writer or DocumentArtifactWriter()).write(
        root,
        selected,  # type: ignore[arg-type]
        **_writer_kwargs(bundle),
    )


def _jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _tsv(path: Path) -> tuple[dict[str, str], ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return tuple(csv.DictReader(handle, dialect="excel-tab"))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_writer_publishes_real_complete_stage7_result_deterministically(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    run = tmp_path / "complete-run"

    assert _publish(run, bundle) == run
    stage = run / "07-document"
    result = bundle.result

    assert (stage / "candidate" / "document.txt").read_text(
        encoding="utf-8"
    ) == result.candidate_text
    assert (stage / "candidate" / "document.md").read_text(
        encoding="utf-8"
    ) == result.candidate_markdown
    assert (stage / "certified" / "document.txt").read_text(
        encoding="utf-8"
    ) == result.text
    assert (stage / "certified" / "document.md").read_text(
        encoding="utf-8"
    ) == result.markdown

    evidence = _jsonl(stage / "records" / "evidence-slices.jsonl")
    segments = _jsonl(stage / "records" / "segments.jsonl")
    units = _jsonl(stage / "records" / "structural-units.jsonl")
    objects = _jsonl(stage / "records" / "objects.jsonl")
    assert [record["slice_id"] for record in evidence] == [
        item.slice_id for item in result.evidence_slices
    ]
    assert [record["output_start"] for record in evidence] == [
        item.output_start for item in result.evidence_slices
    ]
    assert [record["output_stop"] for record in evidence] == [
        item.output_stop for item in result.evidence_slices
    ]
    assert [record["segment_id"] for record in segments] == [
        item.segment_id for item in result.segments
    ]
    assert [record["unit_id"] for record in units] == [
        item.unit_id for item in result.structural_units
    ]
    assert [record["object_id"] for record in objects] == [
        item.object_id for item in result.objects
    ]
    assert [record["slice_id"] for record in _tsv(
        stage / "records" / "evidence-slices.tsv"
    )] == [item.slice_id for item in result.evidence_slices]
    assert [record["segment_id"] for record in _tsv(
        stage / "records" / "segments.tsv"
    )] == [item.segment_id for item in result.segments]
    assert [record["unit_id"] for record in _tsv(
        stage / "records" / "structural-units.tsv"
    )] == [item.unit_id for item in result.structural_units]
    assert [record["object_id"] for record in _tsv(
        stage / "records" / "objects.tsv"
    )] == [item.object_id for item in result.objects]
    assert (stage / "diagnostics.txt").read_text(
        encoding="utf-8"
    ) == "\n".join(result.diagnostics) + "\n"

    second = tmp_path / "same-result"
    _publish(second, bundle)
    assert _tree_bytes(run) == _tree_bytes(second)


def test_writer_publishes_indexed_per_object_and_per_segment_text_trees(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    stage = _publish(tmp_path / "indexed-text", bundle) / "07-document"

    object_dirs = tuple(sorted((stage / "objects").iterdir()))
    segment_dirs = tuple(sorted((stage / "segments").iterdir()))
    assert [path.name for path in object_dirs] == [
        f"object-{index:08d}" for index in range(len(bundle.result.objects))
    ]
    assert [path.name for path in segment_dirs] == [
        f"segment-{index:08d}" for index in range(len(bundle.result.segments))
    ]
    for directory, item in zip(object_dirs, bundle.result.objects):
        assert (directory / "candidate.txt").read_text(
            encoding="utf-8"
        ) == item.candidate_text
        assert (directory / "candidate.md").read_text(
            encoding="utf-8"
        ) == item.candidate_markdown
        assert (directory / "certified.txt").read_text(
            encoding="utf-8"
        ) == item.text
        assert (directory / "certified.md").read_text(
            encoding="utf-8"
        ) == item.markdown
    for directory, item in zip(segment_dirs, bundle.result.segments):
        assert (directory / "candidate.txt").read_text(
            encoding="utf-8"
        ) == item.candidate_text
        assert (directory / "certified.txt").read_text(
            encoding="utf-8"
        ) == item.text


def test_manifest_binds_sources_status_diagnostics_and_every_payload_file(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    stage = _publish(tmp_path / "manifest-run", bundle) / "07-document"
    result = bundle.result
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["semantic_stage"] == 7
    assert manifest["execution_step"] == 7
    assert manifest["stage_name"] == "document-assembly"
    assert manifest["status"] == result.status.value
    assert manifest["source_segment_ids"] == list(result.source_segment_ids)
    assert manifest["source_object_ids"] == list(result.source_object_ids)
    assert manifest["source_block_ids"] == list(result.source_block_ids)
    assert manifest["diagnostics"] == list(result.diagnostics)

    inventory = manifest["files"]
    assert isinstance(inventory, list)
    assert [entry["path"] for entry in inventory] == sorted(
        entry["path"] for entry in inventory
    )
    actual_payloads = {
        path.relative_to(stage).as_posix(): path
        for path in stage.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert {entry["path"] for entry in inventory} == set(actual_payloads)
    for entry in inventory:
        payload = actual_payloads[entry["path"]].read_bytes()
        assert entry["size"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()


def test_real_partial_queue_publishes_candidate_but_no_certified_document(
    tmp_path: Path,
) -> None:
    bundle = _bundle(partial=True)
    assert bundle.queue.status is OcrQueueStatus.PARTIAL
    assert bundle.queue.failed > 0
    assert bundle.result.status is AssemblyStatus.UNRESOLVED
    run = _publish(tmp_path / "unresolved-run", bundle)
    stage = run / "07-document"

    assert (stage / "candidate" / "document.txt").read_text(
        encoding="utf-8"
    ) == bundle.result.candidate_text
    assert (stage / "candidate" / "document.md").read_text(
        encoding="utf-8"
    ) == bundle.result.candidate_markdown
    assert not (stage / "certified" / "document.txt").exists()
    assert not (stage / "certified" / "document.md").exists()
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "unresolved"
    assert manifest["certified"] is False


def test_unattributable_fallback_record_preserves_null_object_id(
    tmp_path: Path,
) -> None:
    fixture = _fallback_fixture(
        _two_object_geometry(),
        _text_only({"block-000000": "fallback text"}),
    )
    result = _assemble_fallback(fixture)
    run = tmp_path / "fallback-run"

    DocumentArtifactWriter().write(
        run,
        result,
        page=fixture.page,
        geometry=fixture.geometry,
        objects=fixture.objects,
        plan=fixture.plan,
        crops=fixture.crops,
        queue=fixture.queue,
        fusion=fixture.fusion,
        planning_config=fixture.planning_config,
    )

    stage = run / "07-document" / "records"
    records = _jsonl(stage / "evidence-slices.jsonl")
    assert records
    assert all(record["object_id"] is None for record in records)
    assert all(record["attribution_level"] == "unattributable" for record in records)
    tsv_records = _tsv(stage / "evidence-slices.tsv")
    assert all(record["object_id"] == "" for record in tsv_records)


def test_writer_rejects_forged_markdown_before_creating_run(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    run = tmp_path / "forged-markdown"

    try:
        forged = replace(
            bundle.result,
            candidate_markdown=bundle.result.candidate_markdown + "\nFORGED",
            markdown=bundle.result.markdown + "\nFORGED",  # type: ignore[operator]
        )
    except ValueError:
        pass
    else:
        with pytest.raises(ValueError, match="result|assembled|Markdown|evidence"):
            _publish(run, bundle, result=forged)

    assert not run.exists()
    assert not tuple(tmp_path.glob(".forged-markdown.partial-*"))


def test_writer_rejects_forged_result_field_before_creating_run(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    forged = replace(
        bundle.result,
        diagnostics=(*bundle.result.diagnostics, "forged-diagnostic"),
    )
    run = tmp_path / "forged-field"

    with pytest.raises(ValueError, match="result|assembled|evidence"):
        _publish(run, bundle, result=forged)

    assert not run.exists()
    assert not tuple(tmp_path.glob(".forged-field.partial-*"))


def test_writer_rejects_forged_source_ids_before_creating_run(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    run = tmp_path / "forged-source"

    try:
        forged = replace(
            bundle.result,
            source_block_ids=("block-forged",),
        )
    except ValueError:
        pass
    else:
        with pytest.raises(ValueError, match="result|assembled|source|evidence"):
            _publish(run, bundle, result=forged)

    assert not run.exists()
    assert not tuple(tmp_path.glob(".forged-source.partial-*"))


def test_manifest_hashing_streams_without_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()

    def forbidden_read_bytes(_path: Path) -> bytes:
        raise AssertionError("artifact hashing must stream files")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    stage = _publish(tmp_path / "streaming-hash", bundle) / "07-document"
    with (stage / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["files"]
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_writer_never_overwrites_a_published_run(tmp_path: Path) -> None:
    bundle = _bundle()
    run = tmp_path / "immutable-run"
    writer = DocumentArtifactWriter()
    _publish(run, bundle, writer=writer)
    before = _tree_bytes(run)

    with pytest.raises(FileExistsError):
        _publish(run, bundle, writer=writer)

    assert _tree_bytes(run) == before
    assert not tuple(tmp_path.glob(".immutable-run.partial-*"))


def test_writer_rolls_back_sibling_partial_tree_on_injected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    run = tmp_path / "broken-run"

    def fail(stage_dir: Path, _result: DocumentAssemblyResult) -> None:
        stage_dir.mkdir(parents=True)
        (stage_dir / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected artifact failure")

    monkeypatch.setattr(DocumentArtifactWriter, "_write_stage", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        _publish(run, bundle)

    assert not run.exists()
    assert not tuple(tmp_path.glob(".broken-run.partial-*"))


def test_concurrent_same_destination_has_one_atomic_winner(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    run = tmp_path / "racing-run"
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    outcomes: list[object] = []

    def publish() -> None:
        barrier.wait()
        try:
            outcome: object = _publish(run, bundle)
        except Exception as exc:
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = tuple(threading.Thread(target=publish) for _ in range(2))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert sum(isinstance(item, Path) for item in outcomes) == 1
    losers = tuple(item for item in outcomes if isinstance(item, Exception))
    assert len(losers) == 1
    assert isinstance(losers[0], OSError)
    assert (run / "07-document" / "manifest.json").is_file()
    assert not tuple(tmp_path.glob(".racing-run.partial-*"))


def test_real_untrusted_segment_ids_are_data_never_filesystem_components(
    tmp_path: Path,
) -> None:
    token = "stage7-artifact-escape-93ac7f"
    segment_ids = (f"../../{token}-a", f"segment/{token}-b")
    bundle = _bundle(segment_ids=segment_ids)
    run = _publish(tmp_path / "safe-run", bundle)

    manifest = json.loads(
        (run / "07-document" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["source_segment_ids"] == list(segment_ids)
    assert not any(token in path.name for path in tmp_path.rglob("*"))
    assert not (tmp_path.parent / f"{token}-a").exists()
    assert not (tmp_path.parent / f"{token}-b").exists()


@pytest.mark.parametrize("unsafe_root", (Path(), Path("..")))
def test_writer_rejects_ambiguous_run_root_before_touching_disk(
    unsafe_root: Path,
) -> None:
    bundle = _bundle()
    with pytest.raises(ValueError, match="root|run|path"):
        _publish(unsafe_root, bundle)


def test_writer_rejects_wrong_boundary_types_before_creating_run(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    writer = DocumentArtifactWriter()
    with pytest.raises(TypeError, match="root"):
        writer.write(
            str(tmp_path / "not-a-path"),  # type: ignore[arg-type]
            bundle.result,
            **_writer_kwargs(bundle),
        )
    with pytest.raises(TypeError, match="result"):
        _publish(tmp_path / "not-a-result", bundle, result=object())
    assert not (tmp_path / "not-a-result").exists()
