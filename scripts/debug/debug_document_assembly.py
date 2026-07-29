#!/usr/bin/env python3
"""Replay the complete sparse document path over a known-text PNG corpus.

The execution order is intentionally fixed to the engine rewrite order:
3 -> 1 -> 6 -> 4 -> 5 -> 2 -> 7.  Ground truth is loaded only after the
Stage 7 evidence tree has been atomically published.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import multiprocessing
import os
import queue as queue_module
import re
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.atomic_publish import rename_no_replace  # noqa: E402
from app.sparse_pipeline.block_crops import (  # noqa: E402
    BlockCropConfig,
    BlockCropPair,
    BlockCropper,
)
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import GeometryResult, GeometryStatus  # noqa: E402
from app.sparse_pipeline.crop_enhancement import (  # noqa: E402
    CropEnhancementConfig,
    CropInput,
    EnhancementBackend,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.document_artifacts import DocumentArtifactWriter  # noqa: E402
from app.sparse_pipeline.document_assembly import (  # noqa: E402
    AssemblyStatus,
    DocumentAssembler,
)
from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.object_reconstruction import (  # noqa: E402
    ObjectReconstructionResult,
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
from app.sparse_pipeline.ocr_queue import OcrLane  # noqa: E402
from app.sparse_pipeline.ocr_session import PersistentOcrSession  # noqa: E402
from app.sparse_pipeline.quality_metrics import (  # noqa: E402
    DEFAULT_MINIMUM_ACCURACY_PERCENT,
    QualityGatePolicy,
    compact_unicode_whitespace as _compact,
    evaluate_quality_gate,
    exact_levenshtein as _levenshtein,
    exact_text_metric as _metric,
)
from app.sparse_pipeline.pipeline_control import (  # noqa: E402
    PIPELINE_ORDER,
    run_pipeline_control,
)
from scripts.debug.debug_report import expected_match  # noqa: E402

ENGINE_CHOICES = ("tesseract", "easy-ru", "easy-zh", "glm")
IGNORED_SUFFIXES = (
    "source.png",
    "aligned.png",
    "-overlay.png",
    ".mask.png",
    ".line-owner.mask.png",
)


@dataclass(frozen=True)
class EngineSettings:
    engines: tuple[str, ...]
    tesseract_executable: str
    tessdata: str | None
    tesseract_psm: int
    tesseract_workers: int
    tesseract_upscale_min_height: int
    tesseract_upscale_max_factor: int
    tesseract_upscale_max_pixels: int
    tesseract_recognition_miss_retry_max_height: int
    tesseract_recognition_miss_retry_padding: int
    easy_python: str
    easy_models: str
    easy_gpu: bool
    glm_python: str
    glm_model: str
    glm_device: str
    glm_dtype: str
    glm_workers: int


@dataclass(frozen=True)
class PreparedItem:
    source: str
    run_id: str
    preparation_seconds: float
    completed_stages: tuple[int, ...]
    stage_seconds: tuple[tuple[int, float], ...]
    geometry_status: str = "error"
    stage4_candidate_sha256: str = ""
    ownership: object | None = None
    page: CropInput | None = None
    geometry: GeometryResult | None = None
    objects: ObjectReconstructionResult | None = None
    plan: BlockPlan | None = None
    crops: tuple[BlockCropPair, ...] = ()
    crop_config: BlockCropConfig | None = None
    planning_config: BlockPlanningConfig | None = None
    error: str = ""


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    geometry_status: str
    assembly_status: str
    elapsed_seconds: float
    completed_stages: tuple[int, ...]
    stage_seconds: tuple[tuple[int, float], ...]
    quality_required: bool = True
    segments: int = 0
    objects: int = 0
    blocks: int = 0
    jobs: int = 0
    complete_jobs: int = 0
    failed_jobs: int = 0
    evidence_slices: int = 0
    structural_units: int = 0
    lost_characters: int | None = None
    reference_characters: int | None = None
    recognized_characters: int | None = None
    accuracy_percent: float | None = None
    legacy_line_recall_percent: float | None = None
    legacy_matched_lines: int | None = None
    legacy_total_lines: int | None = None
    artifact: str = ""
    scoring_error: str = ""
    error: str = ""


class _SessionPool:
    """A fixed set of persistent sessions shared by concurrent page tasks."""

    def __init__(self, settings: EngineSettings, size: int) -> None:
        self._available: queue_module.Queue[PersistentOcrSession] = queue_module.Queue()
        self._sessions = tuple(
            PersistentOcrSession(_lanes(settings)) for _ in range(size)
        )
        for session in self._sessions:
            self._available.put(session)

    def run(
        self,
        *,
        plan: BlockPlan,
        crops: tuple[BlockCropPair, ...],
    ) -> object:
        session = self._available.get()
        try:
            return session.run(plan=plan, crops=crops)
        finally:
            self._available.put(session)

    def close(self) -> None:
        for session in self._sessions:
            session.close()

    def __enter__(self) -> _SessionPool:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _stage3_gate() -> None:
    run_pipeline_control()


def _discover(input_path: Path) -> tuple[Path, ...]:
    if input_path.is_file():
        candidates = (input_path,)
    elif input_path.is_dir():
        candidates = tuple(sorted(input_path.rglob("*.png")))
    else:
        raise FileNotFoundError(input_path)
    return tuple(
        path
        for path in candidates
        if path.is_file()
        and path.suffix.lower() == ".png"
        and not path.name.lower().endswith(IGNORED_SUFFIXES)
    )


def _label(path: Path, root: Path) -> str:
    return path.name if root.is_file() else path.relative_to(root).as_posix()


def _safe_id(path: Path, root: Path) -> str:
    label = _label(path, root)
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.")[:40]
    return f"{digest}-{stem or 'page'}"


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


def _prepare_item(
    source_text: str,
    input_text: str,
    backend_text: str,
) -> PreparedItem:
    source = Path(source_text)
    input_root = Path(input_text)
    run_id = _safe_id(source, input_root)
    started = time.perf_counter()
    completed: list[int] = []
    timings: list[tuple[int, float]] = []

    def stage(number: int, stage_started: float) -> None:
        completed.append(number)
        timings.append((number, time.perf_counter() - stage_started))

    try:
        stage_started = time.perf_counter()
        _stage3_gate()
        stage(3, stage_started)

        stage_started = time.perf_counter()
        with Image.open(source) as opened:
            bundle = GeometryAnalyzer().analyze_bundle(opened)
        geometry = bundle.result
        crop_config = BlockCropConfig(
            enhancement_backend=EnhancementBackend(backend_text)
        )
        page = CropInput(
            f"{run_id}-page",
            _png_bytes(bundle.aligned_rgb, dpi=crop_config.dpi),
        )
        stage(1, stage_started)

        stage_started = time.perf_counter()
        objects = ObjectReconstructor().reconstruct(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            rules=geometry.segmentation.rules,
            matrix=geometry.matrix,
        )
        stage(6, stage_started)

        # Stage 4 is an optional candidate and must not replace the aligned RAW
        # page whose pixel digest is bound into Stage 1 geometry.
        stage_started = time.perf_counter()
        candidate = GammaDarkCropEnhancer(
            CropEnhancementConfig(
                backend=crop_config.enhancement_backend,
                max_input_bytes=crop_config.max_page_bytes,
                max_input_pixels=crop_config.max_page_pixels,
                max_dimension=crop_config.max_dimension,
                max_batch_items=1,
                max_batch_pixels=crop_config.max_page_pixels,
                max_output_bytes=crop_config.max_page_bytes,
                dpi=crop_config.dpi,
            )
        ).enhance(CropInput(f"{run_id}-stage4", page.png_bytes))
        stage(4, stage_started)

        stage_started = time.perf_counter()
        planning_config = BlockPlanningConfig(
            mode=BlockPlanningMode.SPATIAL_2D,
            object_local=True,
        )
        plan = OverlappingBlockPlanner(planning_config).plan(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            objects_result=objects,
            matrix=geometry.matrix,
        )
        crops, rgb_sha256 = BlockCropper(crop_config).crop_with_rgb_sha256(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        if rgb_sha256 != geometry.aligned_rgb_sha256:
            raise RuntimeError("Stage 1 and Stage 5 aligned RGB digests disagree")
        stage(5, stage_started)
        return PreparedItem(
            source=_label(source, input_root),
            run_id=run_id,
            preparation_seconds=time.perf_counter() - started,
            completed_stages=tuple(completed),
            stage_seconds=tuple(timings),
            geometry_status=geometry.status.value,
            stage4_candidate_sha256=candidate.output_sha256,
            ownership=bundle.ownership,
            page=page,
            geometry=geometry,
            objects=objects,
            plan=plan,
            crops=crops,
            crop_config=crop_config,
            planning_config=planning_config,
        )
    except Exception as exc:
        return PreparedItem(
            source=_label(source, input_root),
            run_id=run_id,
            preparation_seconds=time.perf_counter() - started,
            completed_stages=tuple(completed),
            stage_seconds=tuple(timings),
            error=f"{type(exc).__name__}: {exc}",
        )


def _prepared_stream(
    sources: tuple[Path, ...],
    *,
    input_root: Path,
    workers: int,
    window: int,
    executor_kind: str,
    enhancement_backend: str,
) -> Iterator[PreparedItem]:
    executor_type = (
        concurrent.futures.ProcessPoolExecutor
        if executor_kind == "process"
        else concurrent.futures.ThreadPoolExecutor
    )
    kwargs = (
        {"mp_context": multiprocessing.get_context("spawn")}
        if executor_kind == "process"
        else {}
    )
    with executor_type(max_workers=workers, **kwargs) as executor:
        iterator = iter(sources)
        pending: set[concurrent.futures.Future[PreparedItem]] = set()
        for source in iterator:
            pending.add(
                executor.submit(
                    _prepare_item,
                    str(source),
                    str(input_root),
                    enhancement_backend,
                )
            )
            if len(pending) >= window:
                break
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                yield future.result()
                try:
                    source = next(iterator)
                except StopIteration:
                    continue
                pending.add(
                    executor.submit(
                        _prepare_item,
                        str(source),
                        str(input_root),
                        enhancement_backend,
                    )
                )


def _reference(
    input_root: Path,
    source_label: str,
    reference_root: Path | None,
) -> str | None:
    source = input_root if input_root.is_file() else input_root / source_label
    candidates = [source.with_suffix(".txt")]
    if reference_root is not None:
        label = Path(source_label)
        candidates.extend(
            (
                reference_root / f"{source_label}.md",
                reference_root / f"{source_label}.txt",
                (reference_root / label).with_suffix(".md"),
                (reference_root / label).with_suffix(".txt"),
            )
        )
    existing = tuple(dict.fromkeys(path for path in candidates if path.is_file()))
    if not existing:
        return None
    values = tuple(path.read_text(encoding="utf-8") for path in existing)
    if any(value != values[0] for value in values[1:]):
        raise ValueError(
            "ambiguous reference files disagree: "
            + ", ".join(str(path) for path in existing)
        )
    return values[0]


def _is_evidence_only(source: str, configured: tuple[str, ...]) -> bool:
    """Match an explicit unsupported-content source by label or basename."""

    return source in configured or Path(source).name in configured


def _failed_item(
    prepared: PreparedItem,
    error: str | None = None,
    *,
    quality_required: bool = True,
) -> CorpusItem:
    return CorpusItem(
        source=prepared.source,
        status="failed",
        geometry_status=prepared.geometry_status,
        assembly_status="error",
        elapsed_seconds=prepared.preparation_seconds,
        completed_stages=prepared.completed_stages,
        stage_seconds=prepared.stage_seconds,
        quality_required=quality_required,
        error=error or prepared.error or "preparation returned incomplete evidence",
    )


def _finish_item(
    prepared: PreparedItem,
    *,
    input_root: Path,
    reference_root: Path | None,
    corpus_dir: Path,
    sessions: _SessionPool,
    require_reference: bool,
    quality_required: bool = True,
    metric_max_cells: int = 16_000_000,
) -> CorpusItem:
    if (
        prepared.error
        or prepared.page is None
        or prepared.geometry is None
        or prepared.objects is None
        or prepared.plan is None
        or prepared.crop_config is None
    ):
        return _failed_item(prepared, quality_required=quality_required)
    timings = list(prepared.stage_seconds)
    completed = list(prepared.completed_stages)
    finish_started = time.perf_counter()
    try:
        stage_started = time.perf_counter()
        queue = sessions.run(plan=prepared.plan, crops=prepared.crops)
        membership_mode = (
            getattr(prepared.plan, "mode", BlockPlanningMode.FULL_WIDTH)
            is BlockPlanningMode.SPATIAL_2D
        )
        fusion_config = OcrFusionConfig(
            routing_mode=(
                OcrRoutingMode.BLOCK_MEMBERSHIP
                if membership_mode
                else OcrRoutingMode.BBOX_INTERSECTION
            ),
            membership_assume_complete_observations=membership_mode,
        )
        fusion = OcrEvidenceFusion(fusion_config).fuse(
            plan=prepared.plan,
            segments=prepared.geometry.segmentation.segments,
            crops=prepared.crops,
            queue=queue,
        )
        completed.append(2)
        timings.append((2, time.perf_counter() - stage_started))

        stage_started = time.perf_counter()
        result = DocumentAssembler().assemble(
            page=prepared.page,
            geometry=prepared.geometry,
            objects=prepared.objects,
            plan=prepared.plan,
            crops=prepared.crops,
            queue=queue,
            fusion=fusion,
            ownership=prepared.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in prepared.geometry.segmentation.segments
            ),
            planning_config=prepared.planning_config,
            crop_config=prepared.crop_config,
            fusion_config=fusion_config,
        )
        artifact = DocumentArtifactWriter().write(
            corpus_dir / "items" / prepared.run_id,
            result,
            page=prepared.page,
            geometry=prepared.geometry,
            objects=prepared.objects,
            plan=prepared.plan,
            crops=prepared.crops,
            queue=queue,
            fusion=fusion,
            ownership=prepared.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in prepared.geometry.segmentation.segments
            ),
            planning_config=prepared.planning_config,
            crop_config=prepared.crop_config,
            fusion_config=fusion_config,
        )
        completed.append(7)
        timings.append((7, time.perf_counter() - stage_started))

        # This is deliberately after artifact publication. Reference text is
        # scoring-only and never enters OCR, fusion, or document assembly.
        reference = _reference(input_root, prepared.source, reference_root)
        recognized_markdown = (
            result.markdown
            if result.markdown is not None
            else result.candidate_markdown
        )
        metric = None
        legacy_metric: tuple[str, str, str] | None = None
        scoring_error = ""
        if quality_required and reference is not None:
            try:
                metric = _metric(
                    reference,
                    recognized_markdown,
                    max_cells=metric_max_cells,
                )
            except Exception as exc:
                # Scoring is deliberately downstream from immutable Stage 7
                # publication.  A scorer defect makes the quality gate RED,
                # but must not rewrite successful pipeline execution as N/A.
                scoring_error = f"{type(exc).__name__}: {exc}"
            try:
                legacy_metric = expected_match(recognized_markdown, reference)
            except Exception as exc:
                message = f"legacy-line-recall {type(exc).__name__}: {exc}"
                scoring_error = "; ".join(
                    value for value in (scoring_error, message) if value
                )
        no_completed_ocr = bool(prepared.plan.blocks) and queue.complete == 0
        if no_completed_ocr or (
            quality_required and require_reference and reference is None
        ):
            status = "failed"
            error = (
                "all OCR jobs failed"
                if no_completed_ocr
                else "reference text is missing"
            )
        elif (
            result.status is AssemblyStatus.UNRESOLVED
            or prepared.geometry.status is GeometryStatus.DEGRADED
            or fusion.status is OcrFusionStatus.UNRESOLVED
            or queue.failed
        ):
            status = "unresolved"
            error = ""
        else:
            status = "complete"
            error = ""
        return CorpusItem(
            source=prepared.source,
            status=status,
            geometry_status=prepared.geometry_status,
            assembly_status=result.status.value,
            elapsed_seconds=(
                prepared.preparation_seconds
                + time.perf_counter()
                - finish_started
            ),
            completed_stages=tuple(completed),
            stage_seconds=tuple(timings),
            quality_required=quality_required,
            segments=len(prepared.geometry.segmentation.segments),
            objects=len(prepared.objects.objects),
            blocks=len(prepared.plan.blocks),
            jobs=len(queue.jobs),
            complete_jobs=queue.complete,
            failed_jobs=queue.failed,
            evidence_slices=len(result.evidence_slices),
            structural_units=len(result.structural_units),
            lost_characters=metric[0] if metric is not None else None,
            reference_characters=metric[1] if metric is not None else None,
            recognized_characters=metric[2] if metric is not None else None,
            accuracy_percent=metric[3] if metric is not None else None,
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
            artifact=artifact.relative_to(corpus_dir).as_posix(),
            scoring_error=scoring_error,
            error=error,
        )
    except Exception as exc:
        return CorpusItem(
            source=prepared.source,
            status="failed",
            geometry_status=prepared.geometry_status,
            assembly_status="error",
            elapsed_seconds=(
                prepared.preparation_seconds
                + time.perf_counter()
                - finish_started
            ),
            completed_stages=tuple(completed),
            stage_seconds=tuple(timings),
            quality_required=quality_required,
            segments=(
                len(prepared.geometry.segmentation.segments)
                if prepared.geometry is not None
                else 0
            ),
            objects=(
                len(prepared.objects.objects) if prepared.objects is not None else 0
            ),
            blocks=len(prepared.plan.blocks) if prepared.plan is not None else 0,
            error=f"{type(exc).__name__}: {exc}",
        )


def _lanes(settings: EngineSettings) -> tuple[OcrLane, ...]:
    lanes: list[OcrLane] = []
    if "tesseract" in settings.engines:
        lanes.append(
            make_tesseract_lane(
                "tesseract-multilingual",
                config=TesseractConfig(
                    executable=settings.tesseract_executable,
                    tessdata_directory=(
                        Path(settings.tessdata) if settings.tessdata else None
                    ),
                    languages=("eng", "chi_sim", "rus"),
                    psm=settings.tesseract_psm,
                    upscale_min_height=settings.tesseract_upscale_min_height,
                    upscale_max_factor=settings.tesseract_upscale_max_factor,
                    upscale_max_pixels=settings.tesseract_upscale_max_pixels,
                    recognition_miss_retry_max_height=(
                        settings.tesseract_recognition_miss_retry_max_height
                    ),
                    recognition_miss_retry_padding=(
                        settings.tesseract_recognition_miss_retry_padding
                    ),
                ),
                max_workers=settings.tesseract_workers,
            )
        )
    if "easy-ru" in settings.engines:
        lanes.append(
            make_easyocr_lane(
                "easyocr-en-ru",
                config=EasyOcrConfig(
                    ("en", "ru"),
                    Path(settings.easy_models),
                    gpu=settings.easy_gpu,
                    python_executable=Path(settings.easy_python),
                ),
                max_workers=1,
            )
        )
    if "easy-zh" in settings.engines:
        lanes.append(
            make_easyocr_lane(
                "easyocr-zh-en",
                config=EasyOcrConfig(
                    ("ch_sim", "en"),
                    Path(settings.easy_models),
                    gpu=settings.easy_gpu,
                    python_executable=Path(settings.easy_python),
                ),
                max_workers=1,
            )
        )
    if "glm" in settings.engines:
        lanes.append(
            make_glm_ocr_lane(
                "glm-ocr-text",
                config=GlmOcrConfig(
                    Path(settings.glm_model),
                    device=settings.glm_device,
                    dtype=settings.glm_dtype,
                    python_executable=Path(settings.glm_python),
                ),
                max_workers=settings.glm_workers,
            )
        )
    return tuple(lanes)


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = tuple(sorted(values))
    return ordered[round((len(ordered) - 1) * fraction)]


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _write_summary(
    corpus_dir: Path,
    *,
    items: tuple[CorpusItem, ...],
    engines: tuple[str, ...],
    prepare_workers: int,
    ocr_page_workers: int,
    elapsed_seconds: float,
    gate_policy: QualityGatePolicy | None = None,
) -> dict[str, object]:
    policy = gate_policy or QualityGatePolicy()
    failures = sum(item.status == "failed" for item in items)
    unresolved = sum(item.status == "unresolved" for item in items)
    complete = sum(item.status == "complete" for item in items)
    quality_items = tuple(item for item in items if item.quality_required)
    evidence_only_items = tuple(item for item in items if not item.quality_required)
    quality_unresolved = sum(
        item.status == "unresolved" for item in quality_items
    )
    scored = tuple(
        item
        for item in quality_items
        if item.lost_characters is not None
    )
    total_loss = sum(item.lost_characters or 0 for item in scored)
    total_reference = sum(item.reference_characters or 0 for item in scored)
    micro_accuracy = (
        (100.0 if total_loss == 0 else 0.0)
        if total_reference == 0
        else 100.0 * max(0.0, 1.0 - total_loss / total_reference)
    )
    exact_order = bool(items) and all(
        item.completed_stages == PIPELINE_ORDER for item in items
    )
    all_artifacts = bool(items) and all(bool(item.artifact) for item in items)
    gate = evaluate_quality_gate(
        images=len(quality_items),
        scored_images=len(scored),
        failures=failures,
        unresolved=quality_unresolved,
        micro_accuracy_percent=micro_accuracy if scored else None,
        exact_execution_order=exact_order,
        all_artifacts=all_artifacts,
        policy=policy,
    )
    summary: dict[str, object] = {
        "semantic_stage": 7,
        "execution_order": list(PIPELINE_ORDER),
        "status": (
            "failed" if failures else "unresolved" if unresolved else "complete"
        ),
        "gate_status": gate.status,
        "gate_mode": policy.mode,
        "gate_reasons": list(gate.reasons),
        "minimum_accuracy_percent": float(policy.minimum_accuracy_percent),
        "require_full_scoring": policy.require_full_scoring,
        "require_resolved": policy.require_resolved,
        "engines": list(engines),
        "images": len(items),
        "quality_images": len(quality_items),
        "evidence_only_images": len(evidence_only_items),
        "complete": complete,
        "unresolved": unresolved,
        "quality_unresolved": quality_unresolved,
        "evidence_only_unresolved": sum(
            item.status == "unresolved" for item in evidence_only_items
        ),
        "failures": failures,
        "scored_images": len(scored),
        "prepare_workers": prepare_workers,
        "ocr_page_workers": ocr_page_workers,
        "executor": "bounded-process-prepare+thread-session-pool",
        "elapsed_seconds": elapsed_seconds,
        "item_seconds_mean": (
            statistics.fmean(item.elapsed_seconds for item in items)
            if items
            else 0.0
        ),
        "item_seconds_p50": _percentile(
            tuple(item.elapsed_seconds for item in items), 0.50
        ),
        "item_seconds_p95": _percentile(
            tuple(item.elapsed_seconds for item in items), 0.95
        ),
        "accuracy_percent_micro": micro_accuracy if scored else None,
        "accuracy_percent_mean": (
            statistics.fmean(item.accuracy_percent for item in scored)
            if scored
            else None
        ),
        "accuracy_percent_min": (
            min(item.accuracy_percent for item in scored) if scored else None
        ),
        "totals": {
            "segments": sum(item.segments for item in items),
            "objects": sum(item.objects for item in items),
            "blocks": sum(item.blocks for item in items),
            "jobs": sum(item.jobs for item in items),
            "complete_jobs": sum(item.complete_jobs for item in items),
            "failed_jobs": sum(item.failed_jobs for item in items),
            "evidence_slices": sum(item.evidence_slices for item in items),
            "structural_units": sum(item.structural_units for item in items),
            "lost_characters": total_loss,
            "reference_characters": total_reference,
            "recognized_characters": sum(
                item.recognized_characters or 0 for item in scored
            ),
        },
        "invariants": {
            "exact_execution_order": exact_order,
            "all_nonfailed_items_have_stage7_artifacts": all_artifacts,
            "full_reference_scoring": gate.full_scoring,
            "full_quality_reference_scoring": gate.full_scoring,
            "zero_execution_failures": failures == 0,
            "zero_unresolved_items": unresolved == 0,
            "zero_quality_unresolved_items": gate.zero_unresolved,
            "all_items_reached_stage7": exact_order and all_artifacts,
            "micro_accuracy_threshold_met": gate.accuracy_satisfied,
            "reference_loaded_after_artifact_publication": True,
            "unicode_whitespace_only_metric": True,
            "micro_loss_aggregation": True,
        },
        "items": [asdict(item) for item in items],
    }
    _atomic_write(
        corpus_dir / "summary.json",
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    columns = tuple(CorpusItem.__dataclass_fields__)
    rows = ["\t".join(columns)]
    rows.extend(
        "\t".join(
            str(getattr(item, column)).replace("\t", " ").replace("\n", " ")
            for column in columns
        )
        for item in items
    )
    _atomic_write(corpus_dir / "summary.tsv", "\n".join(rows) + "\n")
    markdown = [
        "# Sparse v20 / Stage 7 document corpus",
        "",
        f"Gate: **{summary['gate_status']}**; status: **{summary['status']}**.",
        "",
        (
            f"Gate mode: **{summary['gate_mode']}**; minimum micro accuracy: "
            f"**{summary['minimum_accuracy_percent']}%**; scored: "
            f"**{summary['scored_images']}/{summary['quality_images']} quality "
            f"items**; evidence-only: **{summary['evidence_only_images']}**; "
            f"quality unresolved: **{summary['quality_unresolved']}**."
        ),
        "",
        "Gate reasons: `" + ", ".join(gate.reasons) + "`.",
        "",
        "Execution order: `3 → 1 → 6 → 4 → 5 → 2 → 7`.",
        "",
        (
            f"Micro accuracy: **{summary['accuracy_percent_micro']}%**; "
            f"loss/reference: **{total_loss}/{total_reference}**."
        ),
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    markdown.extend(
        "| "
        + " | ".join(
            str(getattr(item, column)).replace("|", "\\|").replace("\n", "<br>")
            for column in columns
        )
        + " |"
        for item in items
    )
    _atomic_write(corpus_dir / "summary.md", "\n".join(markdown) + "\n")
    return summary


def _engines(value: str) -> tuple[str, ...]:
    engines = tuple(item.strip() for item in value.split(",") if item.strip())
    if not engines or len(engines) != len(set(engines)):
        raise argparse.ArgumentTypeError("engines must be a unique comma-separated list")
    unknown = tuple(item for item in engines if item not in ENGINE_CHOICES)
    if unknown:
        raise argparse.ArgumentTypeError("unknown engines: " + ",".join(unknown))
    return engines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run sparse stages 3->1->6->4->5->2->7 over known-text PNGs"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument(
        "--evidence-only-source",
        action="append",
        default=[],
        metavar="SOURCE",
        help=(
            "run SOURCE through every stage and require its artifacts, but "
            "exclude it from the OCR-quality denominator (repeatable; exact "
            "input-relative label or basename)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "document-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--prepare-workers", type=int, default=min(4, os.cpu_count() or 1)
    )
    parser.add_argument("--prepare-window", type=int, default=8)
    parser.add_argument(
        "--prepare-executor", choices=("process", "thread"), default="process"
    )
    parser.add_argument("--ocr-page-workers", type=int, default=2)
    parser.add_argument("--ocr-window", type=int, default=4)
    parser.add_argument("--engines", type=_engines, default=("tesseract",))
    parser.add_argument(
        "--enhancement-backend",
        choices=tuple(item.value for item in EnhancementBackend),
        default=EnhancementBackend.NUMPY.value,
    )
    parser.add_argument("--tesseract-executable", default="tesseract")
    parser.add_argument(
        "--tessdata",
        type=Path,
        default=REPOSITORY_ROOT / ".cache" / "tessdata_standard",
    )
    parser.add_argument("--tesseract-psm", type=int, choices=(4, 6), default=6)
    parser.add_argument("--tesseract-workers", type=int, default=4)
    parser.add_argument("--tesseract-upscale-min-height", type=int, default=320)
    parser.add_argument("--tesseract-upscale-max-factor", type=int, default=4)
    parser.add_argument(
        "--tesseract-upscale-max-pixels",
        type=int,
        default=16_000_000,
    )
    parser.add_argument(
        "--tesseract-recognition-miss-retry-max-height",
        type=int,
        default=768,
    )
    parser.add_argument(
        "--tesseract-recognition-miss-retry-padding",
        type=int,
        default=32,
    )
    parser.add_argument(
        "--easy-python",
        type=Path,
        default=Path(
            "/home/alpaca/GitHub/IttM-engine-original/ocr/.venv/bin/python"
        ),
    )
    parser.add_argument(
        "--easy-models", type=Path, default=Path("/home/alpaca/.EasyOCR/model")
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
    parser.add_argument(
        "--allow-missing-reference",
        action="store_true",
        help=(
            "allow incomplete reference coverage only for an EXPLORATORY run; "
            "this can never turn missing scores into GREEN"
        ),
    )
    resolution = parser.add_mutually_exclusive_group()
    resolution.add_argument(
        "--allow-unresolved",
        dest="require_resolved",
        action="store_false",
        help=(
            "allow unresolved items only for an EXPLORATORY run; this can never "
            "turn unresolved output into GREEN"
        ),
    )
    resolution.add_argument(
        "--fail-on-unresolved",
        dest="require_resolved",
        action="store_true",
        help="explicitly retain the default strict unresolved policy",
    )
    parser.set_defaults(require_resolved=True)
    parser.add_argument(
        "--minimum-accuracy-percent",
        type=float,
        default=DEFAULT_MINIMUM_ACCURACY_PERCENT,
    )
    parser.add_argument("--metric-max-cells", type=int, default=16_000_000)
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> EngineSettings:
    return EngineSettings(
        engines=args.engines,
        tesseract_executable=args.tesseract_executable,
        tessdata=str(args.tessdata) if args.tessdata is not None else None,
        tesseract_psm=args.tesseract_psm,
        tesseract_workers=args.tesseract_workers,
        tesseract_upscale_min_height=args.tesseract_upscale_min_height,
        tesseract_upscale_max_factor=args.tesseract_upscale_max_factor,
        tesseract_upscale_max_pixels=args.tesseract_upscale_max_pixels,
        tesseract_recognition_miss_retry_max_height=(
            args.tesseract_recognition_miss_retry_max_height
        ),
        tesseract_recognition_miss_retry_padding=(
            args.tesseract_recognition_miss_retry_padding
        ),
        easy_python=str(args.easy_python),
        easy_models=str(args.easy_models),
        easy_gpu=args.easy_device == "cuda",
        glm_python=str(args.glm_python),
        glm_model=str(args.glm_model),
        glm_device=args.glm_device,
        glm_dtype=args.glm_dtype,
        glm_workers=args.glm_workers,
    )


def _validate_args(args: argparse.Namespace) -> None:
    args.evidence_only_source = tuple(args.evidence_only_source)
    if len(args.evidence_only_source) != len(set(args.evidence_only_source)):
        raise ValueError("evidence-only-source values must be unique")
    for name in (
        "prepare_workers",
        "prepare_window",
        "ocr_page_workers",
        "ocr_window",
        "tesseract_workers",
        "tesseract_upscale_max_factor",
        "tesseract_upscale_max_pixels",
        "tesseract_recognition_miss_retry_padding",
        "glm_workers",
        "metric_max_cells",
    ):
        if getattr(args, name) < 1:
            raise ValueError(f"{name.replace('_', '-')} must be positive")
    if args.tesseract_upscale_min_height < 0:
        raise ValueError("tesseract-upscale-min-height must be non-negative")
    if args.tesseract_recognition_miss_retry_max_height < 0:
        raise ValueError(
            "tesseract-recognition-miss-retry-max-height must be non-negative"
        )
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")
    QualityGatePolicy(
        minimum_accuracy_percent=args.minimum_accuracy_percent,
        require_full_scoring=not args.allow_missing_reference,
        require_resolved=args.require_resolved,
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id):
        raise ValueError("run-id contains unsafe characters")


def _execute(
    *,
    sources: tuple[Path, ...],
    input_root: Path,
    reference_root: Path | None,
    corpus_dir: Path,
    args: argparse.Namespace,
) -> tuple[CorpusItem, ...]:
    values: list[CorpusItem] = []
    settings = _settings(args)
    with _SessionPool(settings, args.ocr_page_workers) as sessions:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.ocr_page_workers,
            thread_name_prefix="stage7-page",
        ) as executor:
            pending: set[concurrent.futures.Future[CorpusItem]] = set()

            def drain_one() -> None:
                nonlocal pending
                done, pending = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    item = future.result()
                    values.append(item)
                    print(
                        f"{len(values)}/{len(sources)} {item.status} "
                        f"{item.source} accuracy={item.accuracy_percent}",
                        flush=True,
                    )

            prepared_values = _prepared_stream(
                sources,
                input_root=input_root,
                workers=args.prepare_workers,
                window=args.prepare_window,
                executor_kind=args.prepare_executor,
                enhancement_backend=args.enhancement_backend,
            )
            for prepared in prepared_values:
                while len(pending) >= args.ocr_window:
                    drain_one()
                pending.add(
                    # Handwriting/math inputs still exercise and publish all
                    # seven stages.  They are merely outside the configured
                    # OCR language capability and therefore outside quality.
                    executor.submit(
                        _finish_item,
                        prepared,
                        input_root=input_root,
                        reference_root=reference_root,
                        corpus_dir=corpus_dir,
                        sessions=sessions,
                        require_reference=not args.allow_missing_reference,
                        quality_required=not _is_evidence_only(
                            prepared.source,
                            args.evidence_only_source,
                        ),
                        metric_max_cells=args.metric_max_cells,
                    )
                )
            while pending:
                drain_one()
    return tuple(sorted(values, key=lambda item: item.source))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    input_root = args.input.resolve()
    sources = _discover(input_root)
    if args.limit is not None:
        sources = sources[: args.limit]
    if not sources:
        raise FileNotFoundError(f"no source PNG images at {input_root}")
    source_labels = tuple(_label(source, input_root) for source in sources)
    unknown_evidence_only = tuple(
        configured
        for configured in args.evidence_only_source
        if not any(
            _is_evidence_only(label, (configured,))
            for label in source_labels
        )
    )
    if unknown_evidence_only:
        raise ValueError(
            "evidence-only-source did not match selected input: "
            + ", ".join(unknown_evidence_only)
        )
    reference_root = (
        args.reference_root.resolve() if args.reference_root is not None else None
    )
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / args.run_id
    if final_dir.exists():
        raise FileExistsError(f"debug run already exists: {final_dir}")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{args.run_id}.partial-", dir=output_root)
    )
    started = time.perf_counter()
    try:
        items = _execute(
            sources=sources,
            input_root=input_root,
            reference_root=reference_root,
            corpus_dir=staging,
            args=args,
        )
        summary = _write_summary(
            staging,
            items=items,
            engines=args.engines,
            prepare_workers=args.prepare_workers,
            ocr_page_workers=args.ocr_page_workers,
            elapsed_seconds=time.perf_counter() - started,
            gate_policy=QualityGatePolicy(
                minimum_accuracy_percent=args.minimum_accuracy_percent,
                require_full_scoring=not args.allow_missing_reference,
                require_resolved=args.require_resolved,
            ),
        )
        rename_no_replace(staging, final_dir)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        fatal = {
            "semantic_stage": 7,
            "execution_order": list(PIPELINE_ORDER),
            "status": "failed",
            "gate_status": "RED",
            "gate_mode": (
                "strict"
                if not args.allow_missing_reference and args.require_resolved
                else "exploratory"
            ),
            "minimum_accuracy_percent": args.minimum_accuracy_percent,
            "fatal_error": fatal_error,
        }
        _atomic_write(
            staging / "summary.json",
            json.dumps(fatal, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        _atomic_write(
            staging / "summary.tsv",
            "status\terror\nfailed\t"
            + fatal_error.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            + "\n",
        )
        _atomic_write(
            staging / "summary.md",
            "# Sparse v20 / Stage 7 document corpus\n\n"
            "Gate: **RED**. `"
            + fatal_error.replace("`", "\\`").replace("\r", " ").replace("\n", " ")
            + "`\n",
        )
        rename_no_replace(staging, final_dir)
        print(final_dir, flush=True)
        print(fatal["fatal_error"], file=sys.stderr, flush=True)
        return 1
    print(final_dir, flush=True)
    print(
        f"gate={summary['gate_status']} images={summary['images']} "
        f"complete={summary['complete']} unresolved={summary['unresolved']} "
        f"failures={summary['failures']} "
        f"accuracy_micro={summary['accuracy_percent_micro']}",
        flush=True,
    )
    if summary["gate_status"] not in {"GREEN", "EXPLORATORY"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
