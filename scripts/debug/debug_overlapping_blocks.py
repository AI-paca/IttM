#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import multiprocessing
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.block_artifacts import BlockArtifactWriter  # noqa: E402
from app.sparse_pipeline.block_crops import BlockCropper  # noqa: E402
from app.sparse_pipeline.block_planning import (  # noqa: E402
    BlockPlan,
    BlockPlanningConfig,
    BlockPlanningMode,
    OverlappingBlockPlanner,
)
from app.sparse_pipeline.contracts import GeometryStatus, Segment  # noqa: E402
from app.sparse_pipeline.crop_enhancement import CropInput  # noqa: E402
from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.object_reconstruction import ObjectReconstructor  # noqa: E402
from app.sparse_pipeline.runtime import resolve_sparse_runtime_profile  # noqa: E402

IGNORED_SUFFIXES = ("source.png", "aligned.png", "-overlay.png", ".mask.png")


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    geometry_status: str
    elapsed_seconds: float
    planning_mode: str = ""
    segments: int = 0
    objects: int = 0
    blocks: int = 0
    adjacent_pairs: int = 0
    membership_subblocks: int = 0
    isolated_blocks: int = 0
    masked_segment_references: int = 0
    isolation_mask_bytes: int = 0
    raw_bytes: int = 0
    gamma_bytes: int = 0
    exact_core_partition: bool = False
    adjacent_overlap: bool = False
    physical_rows_atomic: bool = False
    full_width: bool = False
    deterministic_candidates: bool = False
    artifact: str = ""
    error: str = ""


def _safe_id(path: Path, root: Path) -> str:
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.")[:40]
    return f"{digest}-{stem or 'page'}"


def _label(path: Path, root: Path) -> str:
    return path.name if root.is_file() else path.relative_to(root).as_posix()


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


def _physical_rows(segments: tuple[Segment, ...]) -> tuple[tuple[str, ...], ...]:
    rows: list[list[Segment]] = []
    current_top = 0
    current_bottom = 0
    for segment in segments:
        if not rows:
            rows.append([segment])
            current_top = segment.bbox.top
            current_bottom = segment.bbox.bottom
            continue
        if max(current_top, segment.bbox.top) < min(
            current_bottom, segment.bbox.bottom
        ):
            rows[-1].append(segment)
            current_top = min(current_top, segment.bbox.top)
            current_bottom = max(current_bottom, segment.bbox.bottom)
        else:
            rows.append([segment])
            current_top = segment.bbox.top
            current_bottom = segment.bbox.bottom
    return tuple(tuple(item.segment_id for item in row) for row in rows)


def _overlap_graph_connected(plan: BlockPlan) -> bool:
    blocks = tuple(plan.blocks)
    if len(blocks) <= 1:
        return True
    block_index = {item.block_id: index for index, item in enumerate(blocks)}
    graph = {index: set() for index in range(len(blocks))}
    for algebra in plan.adjacent_algebra:
        if not algebra.intersection_segment_ids:
            return False
        first = block_index[algebra.first_block_id]
        second = block_index[algebra.second_block_id]
        graph[first].add(second)
        graph[second].add(first)
    scopes: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        scope_id = block.scope_id or "full-width"
        scopes.setdefault(scope_id, []).append(index)
    for indexes in scopes.values():
        literal_singletons = all(
            len(blocks[index].segment_ids) == 1
            and blocks[index].core_segment_ids == blocks[index].segment_ids
            and not blocks[index].context_segment_ids
            for index in indexes
        )
        if literal_singletons:
            continue
        reached = {indexes[0]}
        pending = [indexes[0]]
        while pending:
            current = pending.pop()
            for neighbour in graph[current] - reached:
                if neighbour in indexes:
                    reached.add(neighbour)
                    pending.append(neighbour)
        if reached != set(indexes):
            return False
    return True


def _run_item(
    source_text: str,
    input_text: str,
    corpus_text: str,
    planning_mode_text: str,
) -> CorpusItem:
    source = Path(source_text)
    input_root = Path(input_text)
    corpus_dir = Path(corpus_text)
    label = _label(source, input_root)
    started = time.perf_counter()
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
        planning_mode = BlockPlanningMode(planning_mode_text)
        runtime_profile = resolve_sparse_runtime_profile()
        planning_config = (
            runtime_profile.block_planning
            if planning_mode is BlockPlanningMode.SPATIAL_2D
            else BlockPlanningConfig(mode=planning_mode)
        )
        planner = OverlappingBlockPlanner(planning_config)
        plan = planner.plan(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            objects_result=objects,
            matrix=geometry.matrix,
        )
        repeated_plan = planner.plan(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            objects_result=objects,
            matrix=geometry.matrix,
        )
        page = CropInput(
            f"{_safe_id(source, input_root)}-page", _png_bytes(bundle.aligned_rgb)
        )
        cropper = BlockCropper(runtime_profile.block_crops)
        crops = cropper.crop(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        repeated = cropper.crop(
            page,
            aligned_size=geometry.segmentation.aligned_size,
            plan=plan,
            ownership=bundle.ownership,
            ownership_segment_ids=tuple(
                segment.segment_id
                for segment in geometry.segmentation.segments
            ),
        )
        deterministic = plan == repeated_plan and crops == repeated
        core_ids = tuple(
            segment_id for block in plan.blocks for segment_id in block.core_segment_ids
        )
        exact_core = (
            core_ids == plan.source_segment_ids
            if planning_mode is BlockPlanningMode.FULL_WIDTH
            else len(core_ids) == len(set(core_ids))
            and set(core_ids) == set(plan.source_segment_ids)
        )
        adjacent_overlap = _overlap_graph_connected(plan)
        core_owner = {
            segment_id: block.block_id
            for block in plan.blocks
            for segment_id in block.core_segment_ids
        }
        source_order = {
            segment_id: index
            for index, segment_id in enumerate(plan.source_segment_ids)
        }
        canonical_segments = tuple(
            sorted(
                geometry.segmentation.segments,
                key=lambda item: source_order[item.segment_id],
            )
        )
        rows_atomic = all(
            len({core_owner[item] for item in row}) == 1
            for row in _physical_rows(canonical_segments)
        )
        full_width = all(
            block.bbox.left == 0
            and block.bbox.right == geometry.segmentation.aligned_size[0]
            for block in plan.blocks
        )
        required = (deterministic, exact_core, adjacent_overlap)
        if planning_mode is BlockPlanningMode.FULL_WIDTH:
            required += (rows_atomic, full_width)
        if not all(required):
            raise RuntimeError("stage 5 block invariant failed")
        run_id = _safe_id(source, input_root)
        artifact = BlockArtifactWriter().write(
            corpus_dir / "items",
            run_id=run_id,
            page=page,
            plan=plan,
            crops=crops,
            matrix=geometry.matrix,
        )
        status = (
            "complete" if geometry.status is GeometryStatus.COMPLETE else "degraded"
        )
        return CorpusItem(
            source=label,
            status=status,
            geometry_status=geometry.status.value,
            elapsed_seconds=time.perf_counter() - started,
            planning_mode=plan.mode.value,
            segments=len(plan.source_segment_ids),
            objects=len(objects.objects),
            blocks=len(plan.blocks),
            adjacent_pairs=len(plan.adjacent_algebra),
            membership_subblocks=sum(
                len(item.segment_ids) > 1
                for item in plan.membership_units
            ),
            isolated_blocks=sum(
                item.isolation_mask_png is not None for item in crops
            ),
            masked_segment_references=sum(
                len(item.masked_segment_ids) for item in crops
            ),
            isolation_mask_bytes=sum(
                len(item.isolation_mask_png)
                for item in crops
                if item.isolation_mask_png is not None
            ),
            raw_bytes=sum(len(item.raw.png_bytes) for item in crops),
            gamma_bytes=sum(len(item.gamma.png_bytes) for item in crops),
            exact_core_partition=exact_core,
            adjacent_overlap=adjacent_overlap,
            physical_rows_atomic=rows_atomic,
            full_width=full_width,
            deterministic_candidates=deterministic,
            artifact=artifact.relative_to(corpus_dir).as_posix(),
        )
    except Exception as exc:  # retain every per-page failure for debug
        return CorpusItem(
            source=label,
            status="failed",
            geometry_status="error",
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = tuple(sorted(values))
    return ordered[round((len(ordered) - 1) * fraction)]


def _write_summaries(
    corpus_dir: Path,
    *,
    values: tuple[CorpusItem, ...],
    workers: int,
    elapsed_seconds: float,
) -> dict[str, object]:
    failures = sum(item.status == "failed" for item in values)
    degraded = sum(item.status == "degraded" for item in values)
    durations = tuple(item.elapsed_seconds for item in values)
    summary: dict[str, object] = {
        "semantic_stage": 5,
        "execution_step": 5,
        "status": "failed" if failures else "degraded" if degraded else "complete",
        "images": len(values),
        "complete": len(values) - failures - degraded,
        "degraded": degraded,
        "failures": failures,
        "workers": workers,
        "executor": "process",
        "elapsed_seconds": elapsed_seconds,
        "item_seconds_mean": statistics.fmean(durations) if durations else 0.0,
        "item_seconds_p50": _percentile(durations, 0.50),
        "item_seconds_p95": _percentile(durations, 0.95),
        "totals": {
            "segments": sum(item.segments for item in values),
            "objects": sum(item.objects for item in values),
            "blocks": sum(item.blocks for item in values),
            "adjacent_pairs": sum(item.adjacent_pairs for item in values),
            "membership_subblocks": sum(
                item.membership_subblocks for item in values
            ),
            "isolated_blocks": sum(item.isolated_blocks for item in values),
            "masked_segment_references": sum(
                item.masked_segment_references for item in values
            ),
            "isolation_mask_bytes": sum(
                item.isolation_mask_bytes for item in values
            ),
            "raw_bytes": sum(item.raw_bytes for item in values),
            "gamma_bytes": sum(item.gamma_bytes for item in values),
        },
        "invariants": {
            "exact_core_partition": sum(item.exact_core_partition for item in values),
            "adjacent_overlap": sum(item.adjacent_overlap for item in values),
            "physical_rows_atomic": sum(item.physical_rows_atomic for item in values),
            "full_width": sum(item.full_width for item in values),
            "deterministic_candidates": sum(
                item.deterministic_candidates for item in values
            ),
        },
        "items": [asdict(item) for item in values],
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
        for item in values
    )
    (corpus_dir / "summary.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    markdown = [
        "# Stage 5 overlapping block corpus",
        "",
        f"Status: **{summary['status']}**",
        "",
        (
            f"Images: {len(values)}; complete: {summary['complete']}; "
            f"degraded: {degraded}; failures: {failures}; workers: {workers}."
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
        for item in values
    )
    (corpus_dir / "summary.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stages 1, 6 and 5 on a PNG corpus with process parallelism"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "block-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--planning-mode",
        choices=tuple(item.value for item in BlockPlanningMode),
        default=BlockPlanningMode.SPATIAL_2D.value,
    )
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fail-on-degraded", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("workers must be positive")
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
    started = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=multiprocessing.get_context("spawn"),
    ) as executor:
        futures = tuple(
            executor.submit(
                _run_item,
                str(source),
                str(input_root),
                str(corpus_dir),
                args.planning_mode,
            )
            for source in sources
        )
        values = tuple(
            sorted(
                (
                    future.result()
                    for future in concurrent.futures.as_completed(futures)
                ),
                key=lambda item: item.source,
            )
        )
    summary = _write_summaries(
        corpus_dir,
        values=values,
        workers=args.workers,
        elapsed_seconds=time.perf_counter() - started,
    )
    print(corpus_dir)
    print(
        f"images={summary['images']} complete={summary['complete']} "
        f"degraded={summary['degraded']} failures={summary['failures']} "
        f"workers={args.workers}"
    )
    if summary["failures"]:
        return 1
    if summary["degraded"] and args.fail_on_degraded:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
