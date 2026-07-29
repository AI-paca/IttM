#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.block_crops import BlockCropPair, BlockCropper  # noqa: E402
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import (  # noqa: E402
    GeometryStatus,
    Segment,
    SparseSegmentMatrix,
)
from app.sparse_pipeline.crop_enhancement import CropInput  # noqa: E402
from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.object_reconstruction import ObjectReconstructor  # noqa: E402
from app.sparse_pipeline.ocr_adapters import (  # noqa: E402
    EasyOcrConfig,
    GlmOcrConfig,
    TesseractConfig,
    make_easyocr_lane,
    make_glm_ocr_lane,
    make_tesseract_lane,
)
from app.sparse_pipeline.ocr_artifacts import OcrArtifactWriter  # noqa: E402
from app.sparse_pipeline.ocr_fusion import (  # noqa: E402
    OcrEvidenceFusion,
    OcrFusionConfig,
    OcrRoutingMode,
    align_exact_text,
    compact_ocr_text,
)
from app.sparse_pipeline.ocr_queue import OcrLane  # noqa: E402
from app.sparse_pipeline.ocr_session import PersistentOcrSession  # noqa: E402

IGNORED_SUFFIXES = (
    "source.png",
    "aligned.png",
    "-overlay.png",
    ".mask.png",
)
ENGINE_CHOICES = ("tesseract", "easy-ru", "easy-zh", "glm")


@dataclass(frozen=True)
class PreparedItem:
    source: str
    run_id: str
    geometry_status: str
    preparation_seconds: float
    segments: tuple[Segment, ...] = ()
    plan: BlockPlan | None = None
    matrix: SparseSegmentMatrix | None = None
    crops: tuple[BlockCropPair, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    geometry_status: str
    preparation_seconds: float
    ocr_seconds: float
    total_seconds: float
    segments: int = 0
    blocks: int = 0
    jobs: int = 0
    complete_jobs: int = 0
    failed_jobs: int = 0
    unresolved_segments: int = 0
    unassigned_words: int = 0
    replica_conflicts: int = 0
    lost_characters: int | None = None
    reference_characters: int | None = None
    recognized_characters: int | None = None
    accuracy_percent: float | None = None
    best_block_lane_id: str = ""
    best_block_transform: str = ""
    best_block_lost_characters: int | None = None
    best_block_accuracy_percent: float | None = None
    artifact: str = ""
    error: str = ""


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


def _safe_id(path: Path, root: Path) -> str:
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.")[:40]
    return f"{digest}-{stem or 'page'}"


def _label(path: Path, root: Path) -> str:
    return path.name if root.is_file() else path.relative_to(root).as_posix()


def _png_bytes(rgb: object) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(rgb, mode="RGB")
    try:
        image.save(
            output,
            format="PNG",
            compress_level=9,
            optimize=False,
            dpi=(300, 300),
        )
    finally:
        image.close()
    return output.getvalue()


def _prepare_item(source_text: str, input_text: str) -> PreparedItem:
    source = Path(source_text)
    input_root = Path(input_text)
    started = time.perf_counter()
    run_id = _safe_id(source, input_root)
    try:
        with Image.open(source) as opened:
            bundle = GeometryAnalyzer().analyze_bundle(opened)
        geometry = bundle.result
        objects = ObjectReconstructor().reconstruct(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            rules=geometry.segmentation.rules,
            matrix=geometry.matrix,
        )
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
        page = CropInput(f"{run_id}-page", _png_bytes(bundle.aligned_rgb))
        crops = BlockCropper().crop(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        return PreparedItem(
            source=_label(source, input_root),
            run_id=run_id,
            geometry_status=geometry.status.value,
            preparation_seconds=time.perf_counter() - started,
            segments=geometry.segmentation.segments,
            plan=plan,
            matrix=geometry.matrix,
            crops=crops,
        )
    except Exception as exc:
        return PreparedItem(
            source=_label(source, input_root),
            run_id=run_id,
            geometry_status="error",
            preparation_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _engines(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items or len(items) != len(set(items)):
        raise argparse.ArgumentTypeError("engines must be a unique comma-separated list")
    unknown = tuple(item for item in items if item not in ENGINE_CHOICES)
    if unknown:
        raise argparse.ArgumentTypeError("unknown engines: " + ",".join(unknown))
    return items


def _lanes(args: argparse.Namespace) -> tuple[OcrLane, ...]:
    lanes: list[OcrLane] = []
    if "tesseract" in args.engines:
        lanes.append(
            make_tesseract_lane(
                "tesseract-multilingual",
                config=TesseractConfig(
                    executable=args.tesseract_executable,
                    tessdata_directory=args.tessdata,
                    languages=("eng", "chi_sim", "rus"),
                    psm=args.tesseract_psm,
                ),
                max_workers=args.tesseract_workers,
            )
        )
    easy_gpu = args.easy_device == "cuda"
    if "easy-ru" in args.engines:
        lanes.append(
            make_easyocr_lane(
                "easyocr-en-ru",
                config=EasyOcrConfig(
                    ("en", "ru"),
                    args.easy_models,
                    gpu=easy_gpu,
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
                    gpu=easy_gpu,
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


def _reference(input_root: Path, source_label: str) -> str | None:
    source = input_root if input_root.is_file() else input_root / source_label
    path = source.with_suffix(".txt")
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _metric(reference: str, recognized: str) -> tuple[int, int, int, float]:
    compact_reference = compact_ocr_text(reference)
    compact_recognized = compact_ocr_text(recognized)
    cells = (len(compact_reference) + 1) * (len(compact_recognized) + 1)
    alignment = align_exact_text(
        compact_reference,
        compact_recognized,
        max_cells=max(1, cells),
    )
    reference_characters = len(compact_reference)
    if reference_characters == 0:
        accuracy = 100.0 if alignment.distance == 0 else 0.0
    else:
        accuracy = 100.0 * max(
            0.0,
            1.0 - alignment.distance / reference_characters,
        )
    return (
        alignment.distance,
        reference_characters,
        len(compact_recognized),
        accuracy,
    )


def _run_ocr(
    prepared: PreparedItem,
    *,
    input_root: Path,
    corpus_dir: Path,
    session: PersistentOcrSession,
) -> CorpusItem:
    total_started = time.perf_counter() - prepared.preparation_seconds
    if prepared.error or prepared.plan is None:
        return CorpusItem(
            source=prepared.source,
            status="failed",
            geometry_status=prepared.geometry_status,
            preparation_seconds=prepared.preparation_seconds,
            ocr_seconds=0.0,
            total_seconds=time.perf_counter() - total_started,
            error=prepared.error or "preparation did not return a block plan",
        )
    if (
        prepared.plan.mode is BlockPlanningMode.SPATIAL_2D
        and prepared.matrix is None
    ):
        return CorpusItem(
            source=prepared.source,
            status="failed",
            geometry_status=prepared.geometry_status,
            preparation_seconds=prepared.preparation_seconds,
            ocr_seconds=0.0,
            total_seconds=time.perf_counter() - total_started,
            error="spatial preparation did not return the Stage 1 matrix",
        )
    ocr_started = time.perf_counter()
    try:
        queue = session.run(plan=prepared.plan, crops=prepared.crops)
        membership_mode = prepared.plan.mode is BlockPlanningMode.SPATIAL_2D
        fusion_config = OcrFusionConfig(
            routing_mode=(
                OcrRoutingMode.BLOCK_MEMBERSHIP
                if membership_mode
                else OcrRoutingMode.BBOX_INTERSECTION
            ),
            membership_assume_complete_observations=membership_mode,
        )
        fusion_engine = (
            OcrEvidenceFusion(fusion_config)
            if membership_mode
            else OcrEvidenceFusion()
        )
        fusion = fusion_engine.fuse(
            plan=prepared.plan,
            segments=prepared.segments,
            crops=prepared.crops,
            queue=queue,
        )
        artifact = OcrArtifactWriter().write(
            corpus_dir / "items",
            run_id=prepared.run_id,
            plan=prepared.plan,
            segments=prepared.segments,
            crops=prepared.crops,
            queue=queue,
            fusion=fusion,
            fusion_config=fusion_config,
            matrix=prepared.matrix,
        )
        selected_texts = (
            *(item.selected_text for item in fusion.segments),
            *(item.selected_text for item in fusion.segment_groups),
        )
        recognized = "\n".join(
            item for item in selected_texts if item is not None
        )
        reference = _reference(input_root, prepared.source)
        metric = _metric(reference, recognized) if reference is not None else None
        block_candidates: list[tuple[float, int, str, str]] = []
        if reference is not None and len(prepared.plan.blocks) == 1:
            for job in queue.jobs:
                if job.output is None:
                    continue
                block_metric = _metric(reference, job.output.text)
                block_candidates.append(
                    (
                        block_metric[3],
                        block_metric[0],
                        job.lane_id,
                        job.transform.value,
                    )
                )
        best_block = max(
            block_candidates,
            default=None,
            key=lambda item: (item[0], -item[1], item[2], item[3]),
        )
        status = fusion.status.value
        if prepared.geometry_status != GeometryStatus.COMPLETE.value:
            status = "degraded" if status == "complete" else status
        return CorpusItem(
            source=prepared.source,
            status=status,
            geometry_status=prepared.geometry_status,
            preparation_seconds=prepared.preparation_seconds,
            ocr_seconds=time.perf_counter() - ocr_started,
            total_seconds=time.perf_counter() - total_started,
            segments=len(prepared.plan.source_segment_ids),
            blocks=len(prepared.plan.blocks),
            jobs=len(queue.jobs),
            complete_jobs=queue.complete,
            failed_jobs=queue.failed,
            unresolved_segments=sum(item.unresolved for item in fusion.segments),
            unassigned_words=len(fusion.unassigned_word_observations),
            replica_conflicts=len(fusion.replica_conflicts),
            lost_characters=metric[0] if metric is not None else None,
            reference_characters=metric[1] if metric is not None else None,
            recognized_characters=metric[2] if metric is not None else None,
            accuracy_percent=metric[3] if metric is not None else None,
            best_block_lane_id=best_block[2] if best_block is not None else "",
            best_block_transform=best_block[3] if best_block is not None else "",
            best_block_lost_characters=(
                best_block[1] if best_block is not None else None
            ),
            best_block_accuracy_percent=(
                best_block[0] if best_block is not None else None
            ),
            artifact=artifact.relative_to(corpus_dir).as_posix(),
        )
    except Exception as exc:
        return CorpusItem(
            source=prepared.source,
            status="failed",
            geometry_status=prepared.geometry_status,
            preparation_seconds=prepared.preparation_seconds,
            ocr_seconds=time.perf_counter() - ocr_started,
            total_seconds=time.perf_counter() - total_started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _prepared_stream(
    sources: tuple[Path, ...],
    *,
    input_root: Path,
    workers: int,
    window: int,
    executor_kind: str,
) -> Iterator[PreparedItem]:
    executor_type = (
        concurrent.futures.ProcessPoolExecutor
        if executor_kind == "process"
        else concurrent.futures.ThreadPoolExecutor
    )
    with executor_type(max_workers=workers) as executor:
        source_iterator = iter(sources)
        pending: dict[concurrent.futures.Future[PreparedItem], Path] = {}
        for source in source_iterator:
            pending[
                executor.submit(_prepare_item, str(source), str(input_root))
            ] = source
            if len(pending) >= window:
                break
        while pending:
            done, _ = concurrent.futures.wait(
                pending,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                pending.pop(future)
                yield future.result()
                try:
                    source = next(source_iterator)
                except StopIteration:
                    continue
                pending[
                    executor.submit(_prepare_item, str(source), str(input_root))
                ] = source


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = tuple(sorted(values))
    return ordered[round((len(ordered) - 1) * fraction)]


def _write_summary(
    corpus_dir: Path,
    *,
    items: tuple[CorpusItem, ...],
    engines: tuple[str, ...],
    elapsed_seconds: float,
) -> dict[str, object]:
    failures = sum(item.status == "failed" for item in items)
    unresolved = sum(item.status == "unresolved" for item in items)
    degraded = sum(item.status == "degraded" for item in items)
    scored = tuple(
        item.accuracy_percent
        for item in items
        if item.accuracy_percent is not None
    )
    block_scored = tuple(
        item.best_block_accuracy_percent
        for item in items
        if item.best_block_accuracy_percent is not None
    )
    durations = tuple(item.total_seconds for item in items)
    total_lost_characters = sum(
        item.lost_characters or 0
        for item in items
        if item.lost_characters is not None
    )
    total_reference_characters = sum(
        item.reference_characters or 0
        for item in items
        if item.reference_characters is not None
    )
    scored_items = sum(item.lost_characters is not None for item in items)
    character_accuracy_percent = (
        (100.0 if total_lost_characters == 0 else 0.0)
        if total_reference_characters == 0
        else 100.0
        * max(
            0.0,
            1.0 - total_lost_characters / total_reference_characters,
        )
    )
    summary: dict[str, object] = {
        "semantic_stage": 2,
        "execution_step": 6,
        "status": (
            "failed"
            if failures
            else "unresolved"
            if unresolved
            else "degraded"
            if degraded
            else "complete"
        ),
        "engines": list(engines),
        "images": len(items),
        "complete": sum(item.status == "complete" for item in items),
        "unresolved": unresolved,
        "degraded": degraded,
        "failures": failures,
        "elapsed_seconds": elapsed_seconds,
        "item_seconds_mean": statistics.fmean(durations) if durations else 0.0,
        "item_seconds_p50": _percentile(durations, 0.50),
        "item_seconds_p95": _percentile(durations, 0.95),
        "scored_images": scored_items,
        "accuracy_percent_micro": (
            character_accuracy_percent if scored_items else None
        ),
        "accuracy_percent_mean": statistics.fmean(scored) if scored else None,
        "accuracy_percent_min": min(scored) if scored else None,
        "best_block_accuracy_percent_mean": (
            statistics.fmean(block_scored) if block_scored else None
        ),
        "best_block_accuracy_percent_min": (
            min(block_scored) if block_scored else None
        ),
        "totals": {
            "segments": sum(item.segments for item in items),
            "blocks": sum(item.blocks for item in items),
            "jobs": sum(item.jobs for item in items),
            "complete_jobs": sum(item.complete_jobs for item in items),
            "failed_jobs": sum(item.failed_jobs for item in items),
            "unresolved_segments": sum(item.unresolved_segments for item in items),
            "unassigned_words": sum(item.unassigned_words for item in items),
            "replica_conflicts": sum(item.replica_conflicts for item in items),
            "lost_characters": total_lost_characters,
            "reference_characters": total_reference_characters,
            "recognized_characters": sum(
                item.recognized_characters or 0
                for item in items
                if item.recognized_characters is not None
            ),
        },
        "items": [asdict(item) for item in items],
    }
    (corpus_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
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
    (corpus_dir / "summary.tsv").write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    markdown = [
        "# Stage 2 OCR block corpus",
        "",
        f"Status: **{summary['status']}**",
        "",
        "Engines: " + ", ".join(engines),
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
    (corpus_dir / "summary.md").write_text(
        "\n".join(markdown) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stage 2 OCR lanes over bounded overlapping block crops"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "ocr-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--engines", type=_engines, default=("tesseract",))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prepare-workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--prepare-window", type=int, default=8)
    parser.add_argument(
        "--prepare-executor",
        choices=("thread", "process"),
        default="thread",
    )
    parser.add_argument("--tesseract-executable", default="tesseract")
    parser.add_argument(
        "--tessdata",
        type=Path,
        default=REPOSITORY_ROOT / ".cache" / "tessdata_standard",
    )
    parser.add_argument("--tesseract-psm", type=int, choices=(4, 6), default=6)
    parser.add_argument("--tesseract-workers", type=int, default=8)
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
    parser.add_argument("--fail-on-unresolved", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for name in (
        "prepare_workers",
        "prepare_window",
        "tesseract_workers",
        "glm_workers",
    ):
        if getattr(args, name) < 1:
            raise ValueError(f"{name.replace('_', '-')} must be positive")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id):
        raise ValueError("run-id contains unsafe characters")
    input_root = args.input.resolve()
    sources = _discover(input_root)
    if args.limit is not None:
        sources = sources[: args.limit]
    if not sources:
        raise FileNotFoundError(f"no PNG images at {input_root}")
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    corpus_dir = output_root / args.run_id
    corpus_dir.mkdir(exist_ok=False)
    lanes = _lanes(args)
    started = time.perf_counter()
    values: list[CorpusItem] = []
    with PersistentOcrSession(lanes) as session:
        for prepared in _prepared_stream(
            sources,
            input_root=input_root,
            workers=args.prepare_workers,
            window=args.prepare_window,
            executor_kind=args.prepare_executor,
        ):
            item = _run_ocr(
                prepared,
                input_root=input_root,
                corpus_dir=corpus_dir,
                session=session,
            )
            values.append(item)
            print(
                f"{len(values)}/{len(sources)} {item.status} "
                f"{item.source} accuracy={item.accuracy_percent}",
                flush=True,
            )
    items = tuple(sorted(values, key=lambda item: item.source))
    summary = _write_summary(
        corpus_dir,
        items=items,
        engines=args.engines,
        elapsed_seconds=time.perf_counter() - started,
    )
    print(corpus_dir, flush=True)
    print(
        f"images={summary['images']} complete={summary['complete']} "
        f"unresolved={summary['unresolved']} failures={summary['failures']}",
        flush=True,
    )
    if summary["failures"]:
        return 1
    if summary["unresolved"] and args.fail_on_unresolved:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
