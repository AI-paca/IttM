#!/usr/bin/env python3
"""Run the sparse pipeline as an auditable, illustrated teaching replay.

The reference text is deliberately loaded only after the complete stage tree
has been published.  It can score an OCR result, but it cannot affect geometry,
block planning, OCR fusion, grammar, or document assembly.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.block_crops import (  # noqa: E402
    BlockCropConfig,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import GeometryStatus  # noqa: E402
from app.sparse_pipeline.crop_enhancement import (  # noqa: E402
    CropEnhancementConfig,
    CropInput,
    EnhancementBackend,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.document_assembly import (  # noqa: E402
    AssemblyStatus,
    DocumentAssembler,
    DocumentAssemblyConfig,
)
from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.object_reconstruction import (  # noqa: E402
    ObjectKind,
    ObjectReconstructionConfig,
    ObjectReconstructor,
)
from app.sparse_pipeline.ocr_adapters import (  # noqa: E402
    EasyOcrConfig,
    GlmOcrConfig,
    TesseractConfig,
    make_easyocr_lane,
    make_glm_ocr_lane,
    make_tesseract_lane,
)
from app.sparse_pipeline.ocr_fusion import (  # noqa: E402
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrFusionStatus,
    OcrRoutingMode,
)
from app.sparse_pipeline.ocr_session import PersistentOcrSession  # noqa: E402
from app.sparse_pipeline.ocr_queue import OcrLane  # noqa: E402
from app.sparse_pipeline.pipeline_control import (  # noqa: E402
    PIPELINE_ORDER,
    run_pipeline_control,
)
from app.sparse_pipeline.pipeline_evidence import SparsePipelineEvidence  # noqa: E402
from app.sparse_pipeline.quality_metrics import exact_text_metric  # noqa: E402
from app.sparse_pipeline.tutorial_artifacts import TutorialArtifactWriter  # noqa: E402
from scripts.debug.debug_report import expected_match  # noqa: E402

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENGINE_CHOICES = ("tesseract", "easy-ru", "easy-zh", "glm")
_STAGES = (
    (3, "recursive control", "03-control"),
    (1, "recursive geometry and sparse matrix", "01-geometry"),
    (6, "objects from sparse matrix", "06-objects"),
    (4, "full-page enhancement calibration candidate", "04-enhancement"),
    (5, "overlapping context blocks and set algebra", "05-blocks"),
    (2, "OCR evidence and segment routing", "02-ocr"),
    (7, "object grammar and document assembly", "07-document"),
)


@dataclass(frozen=True)
class TutorialItem:
    source: str
    page: int
    item_id: str
    status: str
    artifact: str
    elapsed_seconds: float
    quality_required: bool = True
    geometry_status: str = "error"
    fusion_status: str = "error"
    assembly_status: str = "error"
    ocr_profile: tuple[str, ...] = ()
    planning_mode: str = ""
    segments: int = 0
    objects: int = 0
    blocks: int = 0
    jobs: int = 0
    complete_jobs: int = 0
    failed_jobs: int = 0
    unassigned_words: int = 0
    unresolved_segments: int = 0
    overlap_conflicts: int = 0
    accuracy_percent: float | None = None
    lost_characters: int | None = None
    reference_characters: int | None = None
    recognized_characters: int | None = None
    legacy_line_recall_percent: float | None = None
    legacy_matched_lines: int | None = None
    legacy_total_lines: int | None = None
    source_sha256: str = ""
    reference_sha256: str = ""
    reference_path: str = ""
    scoring_error: str = ""
    error: str = ""


def _png_bytes(rgb: object, *, dpi: int) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(rgb, mode="RGB")
    try:
        image.save(
            output,
            format="PNG",
            compress_level=9,
            optimize=False,
            dpi=(dpi, dpi),
        )
    finally:
        image.close()
    return output.getvalue()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()


def _display_path(path: Path) -> str:
    """Use a portable repo-relative label, or an absolute external path."""

    try:
        return _relative(path)
    except ValueError:
        return path.resolve().as_posix()


def _artifact_path(stored_path: str) -> Path:
    """Resolve either form emitted by :func:`_display_path`."""

    path = Path(stored_path)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _item_id(source: Path, page: int) -> str:
    # Hash the normalized path, not only its basename.  Recursive `all` runs
    # commonly contain e.g. multiple page.png files in different directories.
    label = f"{_display_path(source)}:page-{page:03d}"
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-.")[:48]
    return f"{digest}-{stem or 'sample'}-page-{page:03d}"


def _validate_unique_sources(sources: tuple[Path, ...]) -> None:
    identities = tuple(_display_path(path) for path in sources)
    if len(identities) != len(set(identities)):
        raise ValueError("input paths must resolve to unique files")


def _source_pages(
    source: Path,
    *,
    pdf_dpi: int,
    pdf_max_pages: int,
) -> Iterator[tuple[int, Image.Image]]:
    if source.suffix.casefold() == ".pdf":
        from pdf2image import convert_from_path

        pages = convert_from_path(
            str(source),
            dpi=pdf_dpi,
            first_page=1,
            last_page=pdf_max_pages,
            fmt="png",
        )
        try:
            for number, page in enumerate(pages, start=1):
                yield number, page.convert("RGB")
        finally:
            for page in pages:
                page.close()
        return
    with Image.open(source) as opened:
        opened.load()
        yield 1, opened.convert("RGB")


def _reference_path(source: Path, reference_root: Path) -> Path | None:
    candidates = (
        reference_root / f"{source.name}.md",
        reference_root / f"{source.name}.txt",
        source.with_suffix(".md"),
        source.with_suffix(".txt"),
    )
    values = tuple(dict.fromkeys(path for path in candidates if path.is_file()))
    if not values:
        return None
    texts = tuple(path.read_text(encoding="utf-8") for path in values)
    if any(text != texts[0] for text in texts[1:]):
        raise ValueError(
            "ambiguous reference files disagree: "
            + ", ".join(str(path) for path in values)
        )
    return values[0]


def _run_page(
    image: Image.Image,
    *,
    source: Path,
    page_number: int,
    item_root: Path,
    reference_root: Path,
    single_context_session: PersistentOcrSession,
    document_context_session: PersistentOcrSession,
    enhancement_backend: EnhancementBackend,
    block_mode: BlockPlanningMode,
    block_padding: int,
    block_max_core_segments: int,
    run_provenance: dict[str, object],
    quality_required: bool = True,
) -> tuple[TutorialItem, str | None]:
    started = time.perf_counter()
    item_id = _item_id(source, page_number)
    try:
        control = run_pipeline_control()

        geometry_bundle = GeometryAnalyzer().analyze_bundle(image)
        geometry = geometry_bundle.result

        object_config = ObjectReconstructionConfig()
        objects = ObjectReconstructor(object_config).reconstruct(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            rules=geometry.segmentation.rules,
            matrix=geometry.matrix,
        )

        crop_config = BlockCropConfig(
            enhancement_backend=enhancement_backend,
        )
        page = CropInput(
            f"{item_id}-page",
            _png_bytes(geometry_bundle.aligned_rgb, dpi=crop_config.dpi),
        )
        stage4_config = CropEnhancementConfig(
            backend=enhancement_backend,
            max_input_bytes=crop_config.max_page_bytes,
            max_input_pixels=crop_config.max_page_pixels,
            max_dimension=crop_config.max_dimension,
            max_batch_items=1,
            max_batch_pixels=crop_config.max_page_pixels,
            max_output_bytes=crop_config.max_page_bytes,
            dpi=crop_config.dpi,
        )
        stage4 = GammaDarkCropEnhancer(stage4_config).enhance(page)

        planning_config = BlockPlanningConfig(
            mode=block_mode,
            max_core_segments=block_max_core_segments,
            padding=block_padding,
            object_local=block_mode is BlockPlanningMode.SPATIAL_2D,
        )
        plan = OverlappingBlockPlanner(planning_config).plan(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            objects_result=objects,
            matrix=geometry.matrix,
        )
        crops, aligned_digest = BlockCropper(crop_config).crop_with_rgb_sha256(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        if aligned_digest != geometry.aligned_rgb_sha256:
            raise RuntimeError("Stage 1 and Stage 5 aligned RGB digests disagree")

        document_context = len(plan.blocks) > 1 or any(
            item.kind is ObjectKind.TABLE for item in objects.objects
        )
        session = (
            document_context_session
            if document_context
            else single_context_session
        )
        ocr_profile = tuple(lane.lane_id for lane in session.lanes)
        queue = session.run(plan=plan, crops=crops)
        fusion_config = OcrFusionConfig(
            routing_mode=(
                OcrRoutingMode.BLOCK_MEMBERSHIP
                if block_mode is BlockPlanningMode.SPATIAL_2D
                else OcrRoutingMode.BBOX_INTERSECTION
            ),
            membership_assume_complete_observations=(
                block_mode is BlockPlanningMode.SPATIAL_2D
            ),
        )
        fusion = OcrEvidenceFusion(fusion_config).fuse(
            plan=plan,
            segments=geometry.segmentation.segments,
            crops=crops,
            queue=queue,
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
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
            object_config=object_config,
            planning_config=planning_config,
            crop_config=crop_config,
            fusion_config=fusion_config,
            stage4_config=stage4_config,
        )

        assembly_config = DocumentAssemblyConfig()
        document = DocumentAssembler(assembly_config).assemble(
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            queue=queue,
            fusion=fusion,
            ownership=geometry_bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
            object_config=object_config,
            planning_config=planning_config,
            crop_config=crop_config,
            fusion_config=fusion_config,
        )
        artifact = TutorialArtifactWriter().write(
            item_root,
            run_id=item_id,
            control=control,
            geometry=geometry_bundle,
            evidence=evidence,
            document=document,
            assembly_config=assembly_config,
            provenance={
                **run_provenance,
                "source_path": _display_path(source),
                "source_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "page_number": page_number,
                "item_id": item_id,
                "configuration": {
                    "enhancement_backend": enhancement_backend.value,
                    "block_mode": block_mode.value,
                    "block_padding": block_padding,
                    "block_max_core_segments": block_max_core_segments,
                    "single_or_document_ocr_profile": list(ocr_profile),
                },
            },
        )

        # Reference evidence enters only after the immutable tutorial tree is
        # published.  This ordering is part of the acceptance contract.
        reference_file = _reference_path(source, reference_root)
        reference = (
            reference_file.read_text(encoding="utf-8")
            if reference_file is not None
            else None
        )
        recognized_text = (
            document.text
            if document.text is not None
            else document.candidate_text
        )
        recognized_markdown = (
            document.markdown
            if document.markdown is not None
            else document.candidate_markdown
        )
        metric = None
        legacy_metric: tuple[str, str, str] | None = None
        scoring_error = ""
        if quality_required and reference is not None:
            try:
                metric = exact_text_metric(reference, recognized_markdown)
            except Exception as exc:
                # The complete tutorial tree already exists.  A scorer defect
                # must make quality RED without relabelling Stage 7 as failed.
                scoring_error = f"{type(exc).__name__}: {exc}"
            try:
                legacy_metric = expected_match(recognized_markdown, reference)
            except Exception as exc:
                message = f"legacy-line-recall {type(exc).__name__}: {exc}"
                scoring_error = "; ".join(
                    value for value in (scoring_error, message) if value
                )
        resolved = (
            geometry.status is GeometryStatus.COMPLETE
            and fusion.status is OcrFusionStatus.COMPLETE
            and document.status is AssemblyStatus.COMPLETE
            and queue.failed == 0
            and not fusion.unassigned_word_observations
        )
        status = (
            "complete"
            if resolved and (reference is not None or not quality_required)
            else "unresolved"
        )
        item = TutorialItem(
            source=_display_path(source),
            page=page_number,
            item_id=item_id,
            status=status,
            artifact=_display_path(artifact),
            elapsed_seconds=time.perf_counter() - started,
            quality_required=quality_required,
            geometry_status=geometry.status.value,
            fusion_status=fusion.status.value,
            assembly_status=document.status.value,
            ocr_profile=ocr_profile,
            planning_mode=plan.mode.value,
            segments=len(geometry.segmentation.segments),
            objects=len(objects.objects),
            blocks=len(plan.blocks),
            jobs=len(queue.jobs),
            complete_jobs=queue.complete,
            failed_jobs=queue.failed,
            unassigned_words=len(fusion.unassigned_word_observations),
            unresolved_segments=sum(item.unresolved for item in fusion.segments),
            overlap_conflicts=sum(
                len(item.conflicting_intersection_segment_ids)
                for item in fusion.overlaps
            ),
            accuracy_percent=metric[3] if metric is not None else None,
            lost_characters=metric[0] if metric is not None else None,
            reference_characters=metric[1] if metric is not None else None,
            recognized_characters=metric[2] if metric is not None else None,
            legacy_line_recall_percent=(
                float(legacy_metric[0])
                if legacy_metric is not None and legacy_metric[0] != "n/a"
                else None
            ),
            legacy_matched_lines=(
                int(legacy_metric[1]) if legacy_metric is not None else None
            ),
            legacy_total_lines=(
                int(legacy_metric[2]) if legacy_metric is not None else None
            ),
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            reference_sha256=(
                hashlib.sha256(reference.encode("utf-8")).hexdigest()
                if reference is not None
                else ""
            ),
            reference_path=(
                _display_path(reference_file)
                if reference_file is not None
                else ""
            ),
            scoring_error=scoring_error,
            error=(
                ""
                if reference is not None or not quality_required
                else "reference text is missing"
            ),
        )
        return item, recognized_text
    except Exception as exc:
        return (
            TutorialItem(
                source=_display_path(source),
                page=page_number,
                item_id=item_id,
                status="failed",
                artifact="",
                elapsed_seconds=time.perf_counter() - started,
                quality_required=quality_required,
                error=f"{type(exc).__name__}: {exc}",
            ),
            None,
        )


def _report_relative(path: Path, *, report_path: Path) -> str:
    """Return a link target relative to the Markdown file containing it."""

    relative = os.path.relpath(
        path.resolve(),
        start=report_path.resolve().parent,
    )
    return Path(relative).as_posix()
def _git_commit() -> str:
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unknown"


def _stage_links(artifact: Path, stage_dir: str) -> tuple[Path, ...]:
    stage = artifact / stage_dir
    if not stage.is_dir():
        return ()
    preferred = (
        "objects-overlay.png",
        "word-box-overlay.png",
        "trace.txt",
        "matrix.txt",
        "reading-order.txt",
        "document.md",
        "document.txt",
        "objects.tsv",
        "segments.tsv",
        "diagnostics.txt",
        "manifest.json",
    )
    selected: list[Path] = []
    for name in preferred:
        direct = stage / name
        if direct.is_file():
            selected.append(direct)
            continue
        candidates = tuple(sorted(stage.rglob(name)))
        if candidates:
            selected.append(candidates[0])
    for relative in ("adjacent-pairs/pair-000000/manifest.json",):
        candidate = stage / relative
        if candidate.is_file():
            selected.append(candidate)
    if not selected:
        selected.extend(
            path
            for path in sorted(stage.rglob("*"))
            if path.is_file()
        )
    return tuple(dict.fromkeys(selected))[:8]


def _json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"gallery manifest has an invalid shape: {path}")
    return value


def _json_items(path: Path) -> tuple[dict[str, object], ...]:
    value = _json_object(path)
    if not value:
        return ()
    if not isinstance(value.get("items"), list):
        raise ValueError(f"gallery manifest has an invalid shape: {path}")
    items = tuple(value["items"])
    if any(not isinstance(item, dict) for item in items):
        raise ValueError(f"gallery manifest contains a non-object item: {path}")
    return items


def _id_list(value: object) -> str:
    if not isinstance(value, list):
        return "invalid"
    return ", ".join(str(item) for item in value) or "—"


def _actual_segment_gallery(
    artifact: Path,
    *,
    report_path: Path | None = None,
    limit: int = 8,
) -> list[str]:
    report_path = report_path or (REPOSITORY_ROOT / "debag-report.md")

    def link(path: Path) -> str:
        return _report_relative(path, report_path=report_path)

    crop_root = artifact / "01-geometry" / "segment-crops"
    items = _json_items(crop_root / "manifest.json")
    if not items:
        return [
            "**Ошибка отчёта:** фактические segment crops не опубликованы; "
            "overlay не считается доказательством сегментации."
        ]
    lines = [
        "#### Фактические сегменты, первые 8",
        "",
        "Ниже не обводка исходника: слева точный bbox crop, справа только "
        "foreground-пиксели, которыми Stage 1 реально владеет для segment_id.",
        "",
        f"[Открыть все {len(items)} сегментов]({link(crop_root / 'gallery.md')}) · "
        f"[manifest]({link(crop_root / 'manifest.json')})",
    ]
    for item in items[:limit]:
        segment_id = str(item.get("segment_id", "invalid-segment"))
        raw = crop_root / str(item.get("raw", "missing"))
        isolated = crop_root / str(item.get("isolated", "missing"))
        lines.extend(
            (
                "",
                f"##### `{segment_id}`",
                "",
                f"bbox=`{item.get('bbox')}`; size=`{item.get('width')}×{item.get('height')}`; "
                f"ink/owned=`{item.get('ink_pixels')}/{item.get('ownership_pixels')}`; "
                f"sparse cells=`{item.get('sparse_cells')}`; "
                f"span=`{item.get('sparse_span')}`.",
                "",
                "| raw bbox crop | isolated ownership crop |",
                "|---|---|",
                f"| ![{segment_id} raw]({link(raw)}) | "
                f"![{segment_id} isolated]({link(isolated)}) |",
            )
        )
    return lines


def _actual_enhancement_gallery(
    artifact: Path,
    *,
    report_path: Path | None = None,
) -> list[str]:
    report_path = report_path or (REPOSITORY_ROOT / "debag-report.md")

    def link(path: Path) -> str:
        return _report_relative(path, report_path=report_path)

    stage = artifact / "04-enhancement"
    production_path = stage / "production-block-manifest.json"
    production = _json_object(production_path)
    items_value = production.get("items") if production else None
    items = (
        tuple(items_value)
        if isinstance(items_value, list)
        and all(isinstance(item, dict) for item in items_value)
        else ()
    )
    if not items:
        return [
            "**Ошибка отчёта:** production per-block raw→gamma Stage 4 не "
            "опубликованы. Full-page calibration не заменяет эти OCR candidates."
        ]
    lines = [
        "#### Production: actual block-local enhancement, первые 8",
        "",
        "Каждый raw block улучшается независимо из-за различающегося фона. "
        "Обе actual версии передаются Stage 2 как OCR candidates.",
        "",
        f"[Все {len(items)} block-local пары]({link(stage / 'production-block-gallery.md')}) · "
        f"[production manifest]({link(production_path)}) · "
        f"invariants=`{production.get('invariants')}`",
    ]
    for item in items[:8]:
        block_id = str(item.get("block_id", "invalid-block"))
        source = stage / str(item.get("source", "missing"))
        output = stage / str(item.get("output", "missing"))
        lines.extend(
            (
                "",
                f"##### `{block_id}`",
                "",
                f"bbox=`{item.get('bbox')}`; size=`{item.get('width')}×{item.get('height')}`; "
                f"recipe/backend=`{item.get('recipe')}/{item.get('backend')}`; "
                f"same geometry=`{item.get('same_geometry')}`.",
                "",
                "| actual raw block | actual enhanced block |",
                "|---|---|",
                f"| ![{block_id} raw]({link(source)}) | "
                f"![{block_id} gamma]({link(output)}) |",
            )
        )

    calibration_path = stage / "manifest.json"
    calibration = _json_object(calibration_path)
    calibration_items = calibration.get("items") if calibration else None
    if (
        isinstance(calibration_items, list)
        and calibration_items
        and isinstance(calibration_items[0], dict)
    ):
        item = calibration_items[0]
        source = stage / str(item.get("source", "missing"))
        output = stage / str(item.get("output", "missing"))
        lines.extend(
            (
                "",
                "#### GLOBAL CALIBRATION ONLY — NOT OCR INPUT",
                "",
                "Full-page source/output ниже нужен только для сверки "
                "recipe/backend и не входит в production OCR critical path.",
                "",
                "| global calibration source | global calibration output |",
                "|---|---|",
                f"| ![global source]({link(source)}) | "
                f"![global output]({link(output)}) |",
                "",
                f"[Global calibration manifest]({link(calibration_path)})",
            )
        )
    return lines


def _actual_block_gallery(
    artifact: Path,
    *,
    report_path: Path | None = None,
    limit: int = 8,
) -> list[str]:
    report_path = report_path or (REPOSITORY_ROOT / "debag-report.md")

    def link(path: Path) -> str:
        return _report_relative(path, report_path=report_path)

    stage = artifact / "05-blocks"
    gallery_path = stage / "crop-gallery.json"
    gallery = _json_object(gallery_path)
    items_value = gallery.get("items") if gallery else None
    items = (
        tuple(items_value)
        if isinstance(items_value, list)
        and all(isinstance(item, dict) for item in items_value)
        else ()
    )
    if not items:
        return [
            "**Ошибка отчёта:** фактические block crops не опубликованы; "
            "page overlay не считается доказательством разбиения на блоки."
        ]
    lines = [
        "#### Actual блоки и memberships, первые 8",
        "",
        "Stage 5 фиксирует actual bbox crop, core/context membership и OR/XOR. "
        "Raw→gamma enhancement показан в Stage 4.",
        "",
        f"[Открыть все {len(items)} блоков и пары]({link(stage / 'membership-gallery.md')}) · "
        f"[manifest]({link(gallery_path)}) · "
        f"invariants=`{gallery.get('invariants')}`",
    ]
    for item in items[:limit]:
        block_id = str(item.get("block_id", "invalid-block"))
        raw = stage / str(item.get("raw", "missing"))
        lines.extend(
            (
                "",
                f"##### `{block_id}`",
                "",
                f"bbox=`{item.get('bbox')}`; size=`{item.get('width')}×{item.get('height')}`; "
                f"core=`{_id_list(item.get('core_segment_ids'))}`; "
                f"context=`{_id_list(item.get('context_segment_ids'))}`; "
                f"members=`{_id_list(item.get('segment_ids'))}`.",
                "",
                f"![{block_id} actual raw block]({link(raw)})",
            )
        )
    pair_root = stage / "adjacent-pairs"
    pair_dirs = tuple(sorted(pair_root.glob("pair-*")))[:8]
    if pair_dirs:
        lines.extend(("", "#### OR/XOR, первые 8 соседних пар"))
    for pair in pair_dirs:
        pair_manifest = _json_object(pair / "manifest.json")
        lines.extend(
            (
                "",
                f"##### `{pair_manifest.get('first_block_id')}` + "
                f"`{pair_manifest.get('second_block_id')}`",
                "",
                f"intersection=`{pair_manifest.get('intersection_segment_ids')}`; "
                f"OR/union=`{pair_manifest.get('union_segment_ids')}`; "
                f"XOR=`{pair_manifest.get('xor_segment_ids')}`. "
                f"[manifest]({link(pair / 'manifest.json')})",
            )
        )
        for operation in ("intersection", "union", "xor"):
            mask = pair / f"{operation}-ink-mask.png"
            if mask.is_file():
                lines.append(f"![{operation} exact ink set]({link(mask)})")
    return lines


def _report_markdown(
    *,
    run_id: str,
    run_dir: Path,
    items: tuple[TutorialItem, ...],
    threshold: float,
    report_path: Path | None = None,
) -> str:
    report_path = report_path or (REPOSITORY_ROOT / "debag-report.md")

    def link(path: Path) -> str:
        return _report_relative(path, report_path=report_path)

    quality_items = tuple(item for item in items if item.quality_required)
    evidence_only_items = tuple(item for item in items if not item.quality_required)
    scored = tuple(
        item
        for item in quality_items
        if item.accuracy_percent is not None
    )
    quality_green = bool(quality_items) and len(scored) == len(quality_items) and all(
        item.accuracy_percent is not None
        and item.accuracy_percent >= threshold
        for item in quality_items
    )
    quality_resolved = bool(quality_items) and all(
        item.status == "complete" for item in quality_items
    )
    evidence_green = bool(items) and all(
        item.status != "failed" and bool(item.artifact) for item in items
    )
    strict_green = evidence_green and quality_resolved and quality_green
    values = [
        "# DEBAG: обучающий прогон sparse engine",
        "",
        f"Run: `{run_id}`. Gate: **{'GREEN' if strict_green else 'RED'}**.",
        f"OCR quality: **{'GREEN' if quality_green else 'RED'}**; "
        f"evidence certification: **{'GREEN' if evidence_green else 'RED'}**.",
        f"Quality scope: **{len(quality_items)}** printed/supported; "
        f"evidence-only: **{len(evidence_only_items)}** unsupported "
        "handwriting/math inputs.",
        "Reference-текст загружается только после атомарной публикации всех "
        "артефактов и используется исключительно для оценки.",
        "`legacy line recall` приведён отдельно только для сопоставления с "
        "debug-all/v1–v19; строгий v20 gate использует exact accuracy.",
        "",
        "Порядок выполнения: `3 → 1 → 6 → 4 → 5 → 2 → 7`.",
        "",
        "| source | page | scope | state | geometry | fusion | assembly | block mode | OCR profile | segments | objects | unresolved | unassigned | conflicts | blocks | jobs | exact accuracy | legacy line recall |",
        "|---|---:|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in items:
        accuracy = (
            f"{item.accuracy_percent:.2f}%"
            if item.accuracy_percent is not None
            else "N/A"
        )
        legacy_recall = (
            f"{item.legacy_line_recall_percent:.2f}% "
            f"({item.legacy_matched_lines}/{item.legacy_total_lines})"
            if item.legacy_line_recall_percent is not None
            else "N/A"
        )
        values.append(
            f"| {item.source} | {item.page} | "
            f"{'quality' if item.quality_required else 'evidence-only'} | "
            f"{item.status} | "
            f"{item.geometry_status} | {item.fusion_status} | "
            f"{item.assembly_status} | {item.planning_mode or 'n/a'} | "
            f"{', '.join(item.ocr_profile) if item.ocr_profile else 'n/a'} | "
            f"{item.segments} | {item.objects} | {item.unresolved_segments} | "
            f"{item.unassigned_words} | "
            f"{item.overlap_conflicts} | "
            f"{item.blocks} | {item.complete_jobs}/{item.jobs} | {accuracy} | "
            f"{legacy_recall} |"
        )
    if scored:
        values.extend(
            (
                "",
                f"Средняя accuracy: `{statistics.fmean(item.accuracy_percent for item in scored):.2f}%`; "
                f"минимум: `{min(item.accuracy_percent for item in scored):.2f}%`; "
                f"строгий порог: `{threshold:.2f}%`.",
            )
        )

    for item in items:
        values.extend(("", f"## {item.source}, page {item.page}: {item.status}"))
        if item.source_sha256:
            values.extend(
                (
                    "",
                    f"Source SHA-256: `{item.source_sha256}`. "
                    f"Reference SHA-256: `{item.reference_sha256 or 'n/a'}`. "
                    f"Reference: `{item.reference_path or 'n/a'}`.",
                )
            )
        if item.error:
            values.extend(("", f"Ошибка/блокер: `{item.error}`"))
        if item.scoring_error:
            values.extend(("", f"Ошибка метрики (pipeline artifacts сохранены): `{item.scoring_error}`"))
        if not item.artifact:
            continue
        artifact = _artifact_path(item.artifact)
        values.extend(
            (
                "",
                f"Полное дерево доказательств: [{item.item_id}/tutorial.md]({link(artifact / 'tutorial.md')}).",
                "",
                f"[Разряженная матрица]({link(artifact / 'sparse-matrix.txt')}) · "
                f"[literal ownership PNG]({link(artifact / 'sparse-matrix-ownership.png')}) · "
                f"[объекты]({link(artifact / 'objects/index.md')}) · "
                f"[stage logs]({link(artifact / 'logs/stages.tsv')}) · "
                f"[provenance]({link(artifact / 'provenance.json')}) · "
                f"[итоговый Markdown]({link(artifact / 'document.md')}).",
            )
        )
        for stage, description, directory in _STAGES:
            values.extend(("", f"### Stage {stage}: {description}", ""))
            if stage == 1:
                values.extend(
                    _actual_segment_gallery(artifact, report_path=report_path)
                )
                values.append("")
            elif stage == 4:
                values.extend(
                    _actual_enhancement_gallery(artifact, report_path=report_path)
                )
                values.append("")
            elif stage == 5:
                values.extend(
                    _actual_block_gallery(artifact, report_path=report_path)
                )
                values.append("")
            links = _stage_links(artifact, directory)
            if not links:
                values.append("Артефакты этапа отсутствуют — это ошибка прогона.")
                continue
            for path in links:
                label = path.relative_to(artifact).as_posix()
                relative = link(path)
                if path.suffix.casefold() == ".png":
                    values.append(f"![{label}]({relative})")
                else:
                    values.append(f"[{label}]({relative})")

    values.extend(
        (
            "",
            "## Как читать доказательства",
            "",
            "Stage 1 встраивает отдельные raw bbox и isolated ownership crops: "
            "isolated PNG содержит только реально присвоенные сегменту пиксели. "
            "Stage 4 показывает actual block-local raw→gamma OCR candidates. "
            "Full-page output помечен GLOBAL CALIBRATION ONLY / NOT OCR INPUT. "
            "Stage 5 показывает actual block bbox, core/context membership и "
            "OR/XOR. Полные galleries содержат каждый сегмент "
            "и каждый блок. Stage 2 показывает наблюдённые bbox OCR; unassigned "
            "evidence всегда делает результат unresolved. Stage 7 сертифицируется "
            "только при полном владении сегментами и отсутствии потерянных "
            "OCR-доказательств.",
            "",
            f"Машиночитаемая сводка: [{run_id}/summary.json]({link(run_dir / 'summary.json')}).",
            "",
        )
    )
    return "\n".join(values)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _engines(value: str) -> tuple[str, ...]:
    engines = tuple(item.strip() for item in value.split(",") if item.strip())
    if not engines or len(engines) != len(set(engines)):
        raise argparse.ArgumentTypeError(
            "engines must be a unique comma-separated list"
        )
    unknown = tuple(item for item in engines if item not in ENGINE_CHOICES)
    if unknown:
        raise argparse.ArgumentTypeError("unknown engines: " + ",".join(unknown))
    return engines


def _ocr_lanes(
    args: argparse.Namespace,
    *,
    document_context: bool,
) -> tuple[OcrLane, ...]:
    """Build a context-specific immutable lane set without starting workers."""

    lanes: list[OcrLane] = []
    if "tesseract" in args.engines:
        psm = (
            args.document_context_psm
            if document_context
            else args.single_context_psm
        )
        lanes.append(
            make_tesseract_lane(
                (
                    f"tesseract-document-context-psm{psm}"
                    if document_context
                    else f"tesseract-single-context-psm{psm}"
                ),
                config=TesseractConfig(
                    executable=args.tesseract_executable,
                    tessdata_directory=args.tessdata,
                    languages=("eng", "chi_sim", "rus"),
                    psm=psm,
                    variables=(
                        (("textord_tabfind_find_tables", "0"),)
                        if document_context
                        else ()
                    ),
                    upscale_min_height=args.tesseract_upscale_min_height,
                    upscale_max_factor=args.tesseract_upscale_max_factor,
                    upscale_max_pixels=args.tesseract_upscale_max_pixels,
                    recognition_miss_retry_max_height=(
                        args.tesseract_recognition_miss_retry_max_height
                    ),
                    recognition_miss_retry_padding=(
                        args.tesseract_recognition_miss_retry_padding
                    ),
                ),
                max_workers=args.tesseract_workers,
            )
        )
    if "easy-ru" in args.engines:
        lanes.append(
            make_easyocr_lane(
                "easyocr-en-ru",
                config=EasyOcrConfig(
                    ("en", "ru"),
                    args.easy_models,
                    gpu=args.easy_device == "cuda",
                    python_executable=args.easy_python,
                ),
                max_workers=1,
            )
        )
    if "easy-zh" in args.engines:
        lanes.append(
            make_easyocr_lane(
                "easyocr-zh-en",
                config=EasyOcrConfig(
                    ("ch_sim", "en"),
                    args.easy_models,
                    gpu=args.easy_device == "cuda",
                    python_executable=args.easy_python,
                ),
                max_workers=1,
            )
        )
    if "glm" in args.engines:
        lanes.append(
            make_glm_ocr_lane(
                "glm-ocr-text",
                config=GlmOcrConfig(
                    args.glm_model,
                    device=args.glm_device,
                    dtype=args.glm_dtype,
                    python_executable=args.glm_python,
                ),
                max_workers=args.glm_workers,
            )
        )
    return tuple(lanes)


def _ocr_provenance(
    args: argparse.Namespace,
    *,
    single_lanes: tuple[OcrLane, ...],
    document_lanes: tuple[OcrLane, ...],
) -> dict[str, object]:
    configuration: dict[str, object] = {
        "engines": list(args.engines),
        "ocr_profile": {
            "single_context": [lane.lane_id for lane in single_lanes],
            "document_context": [lane.lane_id for lane in document_lanes],
        },
    }
    if "tesseract" in args.engines:
        configuration["tesseract"] = {
            "executable": args.tesseract_executable,
            "tessdata": str(args.tessdata) if args.tessdata is not None else None,
            "single_context_psm": args.single_context_psm,
            "document_context_psm": args.document_context_psm,
            "workers": args.tesseract_workers,
            "upscale_min_height": args.tesseract_upscale_min_height,
            "upscale_max_factor": args.tesseract_upscale_max_factor,
            "upscale_max_pixels": args.tesseract_upscale_max_pixels,
            "recognition_miss_retry_max_height": (
                args.tesseract_recognition_miss_retry_max_height
            ),
            "recognition_miss_retry_padding": (
                args.tesseract_recognition_miss_retry_padding
            ),
        }
    if any(engine.startswith("easy-") for engine in args.engines):
        configuration["easyocr"] = {
            "python": str(args.easy_python),
            "models": str(args.easy_models),
            "device": args.easy_device,
            "runtime_downloads": False,
        }
    if "glm" in args.engines:
        configuration["glm"] = {
            "python": str(args.glm_python),
            "model": str(args.glm_model),
            "device": args.glm_device,
            "dtype": args.glm_dtype,
            "workers": args.glm_workers,
        }
    return configuration


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Illustrated full sparse replay for tracked SAMPLE fixtures"
    )
    parser.add_argument("--input", action="append", type=Path, dest="inputs")
    parser.add_argument("--reference-root", type=Path, default=Path("debug/reference"))
    parser.add_argument(
        "--evidence-only-source",
        action="append",
        default=[],
        metavar="SOURCE",
        help=(
            "run SOURCE through all stages and require its artifact tree, but "
            "exclude it from OCR quality (repeatable basename or input path)"
        ),
    )
    parser.add_argument("--output", type=Path, default=Path("debug/tutorial"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--report", type=Path, default=Path("debag-report.md"))
    parser.add_argument("--minimum-accuracy-percent", type=float, default=91.0)
    parser.add_argument("--pdf-dpi", type=int, default=300)
    parser.add_argument("--pdf-max-pages", type=int, default=1)
    parser.add_argument("--enhancement-backend", choices=("auto", "numpy"), default="numpy")
    parser.add_argument("--engines", type=_engines, default=("tesseract",))
    parser.add_argument("--tesseract-executable", default="tesseract")
    parser.add_argument(
        "--tessdata",
        type=Path,
        default=REPOSITORY_ROOT / ".cache" / "tessdata_standard",
    )
    parser.add_argument(
        "--single-context-psm",
        type=int,
        choices=(4, 6),
        default=6,
        help="PSM for a one-block non-table page",
    )
    parser.add_argument(
        "--document-context-psm",
        type=int,
        choices=(4, 6),
        default=4,
        help="PSM for table or overlapping multi-block pages",
    )
    parser.add_argument("--tesseract-workers", type=int, default=4)
    parser.add_argument(
        "--block-mode",
        choices=tuple(item.value for item in BlockPlanningMode),
        default=BlockPlanningMode.SPATIAL_2D.value,
        help="Stage 5 context geometry; spatial_2d is the v20 rewrite",
    )
    parser.add_argument(
        "--block-padding",
        type=int,
        default=24,
        help="OCR block padding in aligned page pixels",
    )
    parser.add_argument(
        "--block-max-core-segments",
        type=int,
        default=24,
        help="Bound core blocks while keeping multi-row document context",
    )
    parser.add_argument("--tesseract-upscale-min-height", type=int, default=320)
    parser.add_argument("--tesseract-upscale-max-factor", type=int, default=4)
    parser.add_argument("--tesseract-upscale-max-pixels", type=int, default=16_000_000)
    parser.add_argument("--tesseract-recognition-miss-retry-max-height", type=int, default=768)
    parser.add_argument("--tesseract-recognition-miss-retry-padding", type=int, default=32)
    parser.add_argument(
        "--easy-python",
        type=Path,
        default=Path(
            "/home/alpaca/GitHub/IttM-engine-original/ocr/.venv/bin/python"
        ),
    )
    parser.add_argument(
        "--easy-models",
        type=Path,
        default=Path("/home/alpaca/.EasyOCR/model"),
    )
    parser.add_argument("--easy-device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument(
        "--glm-python",
        type=Path,
        default=Path("/home/alpaca/tmp-translate/.glmocr-venv/bin/python"),
    )
    parser.add_argument(
        "--glm-model",
        type=Path,
        default=Path(
            "/home/alpaca/.cache/huggingface/hub/"
            "models--zai-org--GLM-OCR/snapshots/"
            "ca5d8b3e287e52589e37c28385d9655ee4372f9d"
        ),
    )
    parser.add_argument("--glm-device", default="cuda:0")
    parser.add_argument(
        "--glm-dtype",
        choices=("bfloat16", "float16", "float32"),
        default="bfloat16",
    )
    parser.add_argument("--glm-workers", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _SAFE_ID.fullmatch(args.run_id):
        raise ValueError("run-id contains unsafe characters")
    if not 0.0 <= args.minimum_accuracy_percent <= 100.0:
        raise ValueError("minimum accuracy must be between zero and 100")
    if (
        args.pdf_dpi < 1
        or args.pdf_max_pages < 1
        or args.tesseract_workers < 1
        or args.glm_workers < 1
    ):
        raise ValueError("DPI, page count and worker count must be positive")
    if args.block_padding < 0:
        raise ValueError("block padding must be non-negative")
    if args.block_max_core_segments < 1:
        raise ValueError("block max core segments must be positive")
    sources = tuple(
        args.inputs
        or (
            Path("debug/fixtures/SAMPLE_4k.png"),
            Path("debug/fixtures/SAMPLE_mixed_ru_en_zh_table_image.pdf"),
        )
    )
    missing = tuple(path for path in sources if not path.is_file())
    if missing:
        raise FileNotFoundError(", ".join(str(path) for path in missing))
    _validate_unique_sources(sources)
    evidence_only_sources = tuple(args.evidence_only_source)
    if len(evidence_only_sources) != len(set(evidence_only_sources)):
        raise ValueError("evidence-only-source values must be unique")

    def quality_required(source: Path) -> bool:
        return not any(
            configured in (source.name, str(source), _display_path(source))
            for configured in evidence_only_sources
        )

    unknown_evidence_only = tuple(
        configured
        for configured in evidence_only_sources
        if not any(
            configured in (source.name, str(source), _display_path(source))
            for source in sources
        )
    )
    if unknown_evidence_only:
        raise ValueError(
            "evidence-only-source did not match selected input: "
            + ", ".join(unknown_evidence_only)
        )
    run_dir = args.output / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    item_root = run_dir / "items"
    item_root.mkdir()
    single_lanes = _ocr_lanes(args, document_context=False)
    document_lanes = _ocr_lanes(args, document_context=True)
    ocr_provenance = _ocr_provenance(
        args,
        single_lanes=single_lanes,
        document_lanes=document_lanes,
    )
    effective_argv = (
        tuple(sys.argv)
        if argv is None
        else (str(Path(__file__).resolve()), *argv)
    )
    run_provenance: dict[str, object] = {
        "git_commit": _git_commit(),
        "argv": list(effective_argv),
        "working_directory": Path.cwd().resolve().as_posix(),
        "run_id": args.run_id,
        "ocr": ocr_provenance,
    }
    backend = EnhancementBackend(args.enhancement_backend)
    results: list[TutorialItem] = []
    recognized: dict[str, str] = {}
    started = time.perf_counter()
    with (
        PersistentOcrSession(single_lanes) as single_context_session,
        PersistentOcrSession(document_lanes) as document_context_session,
    ):
        for source in sources:
            for page_number, image in _source_pages(
                source,
                pdf_dpi=args.pdf_dpi,
                pdf_max_pages=args.pdf_max_pages,
            ):
                try:
                    item, text = _run_page(
                        image,
                        source=source,
                        page_number=page_number,
                        item_root=item_root,
                        reference_root=args.reference_root,
                        single_context_session=single_context_session,
                        document_context_session=document_context_session,
                        enhancement_backend=backend,
                        block_mode=BlockPlanningMode(args.block_mode),
                        block_padding=args.block_padding,
                        block_max_core_segments=args.block_max_core_segments,
                        run_provenance=run_provenance,
                        quality_required=quality_required(source),
                    )
                finally:
                    image.close()
                results.append(item)
                if text is not None:
                    recognized[item.item_id] = text
                print(
                    f"{len(results)} {item.status} {item.source} "
                    f"page={item.page} accuracy={item.accuracy_percent}",
                    flush=True,
                )

    items = tuple(results)
    quality_items = tuple(item for item in items if item.quality_required)
    evidence_only_items = tuple(item for item in items if not item.quality_required)
    quality_green = bool(quality_items) and all(
        item.accuracy_percent is not None
        and item.accuracy_percent >= args.minimum_accuracy_percent
        for item in quality_items
    )
    quality_resolved = bool(quality_items) and all(
        item.status == "complete" for item in quality_items
    )
    evidence_green = bool(items) and all(
        item.status != "failed" and bool(item.artifact) for item in items
    )
    strict_green = evidence_green and quality_resolved and quality_green
    summary = {
        "semantic_pipeline": "recursive-sparse-document",
        "run_id": args.run_id,
        "execution_order": list(PIPELINE_ORDER),
        "gate_status": "GREEN" if strict_green else "RED",
        "quality_gate_status": "GREEN" if quality_green else "RED",
        "evidence_gate_status": "GREEN" if evidence_green else "RED",
        "minimum_accuracy_percent": args.minimum_accuracy_percent,
        "reference_loaded_after_artifact_publication": True,
        "ocr": ocr_provenance,
        "elapsed_seconds": time.perf_counter() - started,
        "images": len(items),
        "quality_images": len(quality_items),
        "evidence_only_images": len(evidence_only_items),
        "complete": sum(item.status == "complete" for item in items),
        "unresolved": sum(item.status == "unresolved" for item in items),
        "quality_unresolved": sum(
            item.status == "unresolved" for item in quality_items
        ),
        "failures": sum(item.status == "failed" for item in items),
        "items": [asdict(item) for item in items],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    for item_id, text in recognized.items():
        path = run_dir / "recognized" / f"{item_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    document_text_parts: list[str] = []
    document_markdown_parts: list[str] = []
    for item in items:
        if not item.artifact:
            continue
        artifact = _artifact_path(item.artifact)
        text_path = artifact / "document.txt"
        markdown_path = artifact / "document.md"
        if text_path.is_file():
            document_text_parts.append(text_path.read_text(encoding="utf-8").rstrip("\n"))
        if markdown_path.is_file():
            document_markdown_parts.append(
                markdown_path.read_text(encoding="utf-8").rstrip("\n")
            )
    (run_dir / "document.txt").write_text(
        "\n\n".join(document_text_parts)
        + ("\n" if document_text_parts else ""),
        encoding="utf-8",
    )
    (run_dir / "document.md").write_text(
        "\n\n---\n\n".join(document_markdown_parts)
        + ("\n" if document_markdown_parts else ""),
        encoding="utf-8",
    )
    (run_dir / "provenance.json").write_text(
        json.dumps(
            {
                **run_provenance,
                "execution_order": list(PIPELINE_ORDER),
                "inputs": [
                    {
                        "path": _display_path(source),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                    for source in sources
                ],
                "artifacts": [item.artifact for item in items if item.artifact],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    run_report_path = run_dir / "debag-report.md"
    run_report = _report_markdown(
        run_id=args.run_id,
        run_dir=run_dir,
        items=items,
        threshold=args.minimum_accuracy_percent,
        report_path=run_report_path,
    )
    root_report = _report_markdown(
        run_id=args.run_id,
        run_dir=run_dir,
        items=items,
        threshold=args.minimum_accuracy_percent,
        report_path=args.report,
    )
    run_report_path.write_text(run_report, encoding="utf-8")
    _write_atomic(args.report, root_report)
    print(args.report.resolve())
    print(f"gate={'GREEN' if strict_green else 'RED'} images={len(items)}")
    return 0 if strict_green else 2


if __name__ == "__main__":
    raise SystemExit(main())
