#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.contracts import GeometryStatus  # noqa: E402
from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.object_artifacts import ObjectArtifactWriter  # noqa: E402
from app.sparse_pipeline.object_reconstruction import ObjectReconstructor  # noqa: E402

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"})
IGNORED_IMAGE_SUFFIXES = (
    ".mask.png",
    ".line-owner.mask.png",
    "-overlay.png",
    "source.png",
    "aligned.png",
)


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    geometry_status: str
    elapsed_seconds: float
    segments: int = 0
    rules: int = 0
    sparse_cells: int = 0
    objects: int = 0
    paragraphs: int = 0
    lists: int = 0
    tables: int = 0
    unknown: int = 0
    exact_partition: bool = False
    artifact: str = ""
    error: str = ""


def _safe_run_id(path: Path, root: Path) -> str:
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-.")[:48] or "image"
    return f"{digest}-{stem}"


def _source_label(path: Path, root: Path) -> str:
    return path.name if root.is_file() else path.relative_to(root).as_posix()


def _discover(input_path: Path) -> tuple[Path, ...]:
    if input_path.is_file():
        candidates = (input_path,)
    elif input_path.is_dir():
        candidates = tuple(sorted(input_path.rglob("*")))
    else:
        raise FileNotFoundError(input_path)
    return tuple(
        path
        for path in candidates
        if path.is_file()
        and path.suffix.lower() in IMAGE_SUFFIXES
        and not path.name.lower().endswith(IGNORED_IMAGE_SUFFIXES)
    )


def _run_item(source_text: str, input_text: str, corpus_text: str) -> CorpusItem:
    source = Path(source_text)
    input_root = Path(input_text)
    corpus_dir = Path(corpus_text)
    label = _source_label(source, input_root)
    started = time.perf_counter()
    try:
        with Image.open(source) as opened:
            opened.load()
            geometry = GeometryAnalyzer().analyze(opened)
        result = ObjectReconstructor().reconstruct(
            aligned_size=geometry.segmentation.aligned_size,
            segments=geometry.segmentation.segments,
            rules=geometry.segmentation.rules,
            matrix=geometry.matrix,
        )
        owned = tuple(
            segment_id
            for document_object in result.objects
            for segment_id in document_object.segment_ids
        )
        exact_partition = (
            len(owned) == len(set(owned))
            and set(owned) == set(result.source_segment_ids)
            and len(result.segment_ownership) == len(result.source_segment_ids)
        )
        if not exact_partition:
            raise RuntimeError("stage 6 did not produce an exact segment partition")
        run_id = _safe_run_id(source, input_root)
        artifact = ObjectArtifactWriter().write(
            corpus_dir / "items",
            run_id=run_id,
            result=result,
        )
        kinds = tuple(item.kind.value for item in result.objects)
        geometry_status = geometry.status.value
        status = (
            "complete" if geometry.status is GeometryStatus.COMPLETE else "degraded"
        )
        return CorpusItem(
            source=label,
            status=status,
            geometry_status=geometry_status,
            elapsed_seconds=time.perf_counter() - started,
            segments=len(geometry.segmentation.segments),
            rules=len(geometry.segmentation.rules),
            sparse_cells=len(geometry.matrix.cells),
            objects=len(result.objects),
            paragraphs=kinds.count("paragraph"),
            lists=kinds.count("list"),
            tables=kinds.count("table"),
            unknown=kinds.count("unknown"),
            exact_partition=True,
            artifact=artifact.relative_to(corpus_dir).as_posix(),
        )
    except Exception as exc:  # debug corpus must retain every per-item failure
        return CorpusItem(
            source=label,
            status="failed",
            geometry_status="error",
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _write_summaries(
    corpus_dir: Path,
    *,
    values: tuple[CorpusItem, ...],
    workers: int,
    executor: str,
    elapsed_seconds: float,
) -> dict[str, object]:
    failures = sum(item.status == "failed" for item in values)
    degraded = sum(item.status == "degraded" for item in values)
    complete = sum(item.status == "complete" for item in values)
    exact = sum(item.exact_partition for item in values)
    summary: dict[str, object] = {
        "semantic_stage": 6,
        "execution_step": 3,
        "status": "failed" if failures else "degraded" if degraded else "complete",
        "images": len(values),
        "complete": complete,
        "degraded": degraded,
        "failures": failures,
        "exact_partitions": exact,
        "workers": workers,
        "executor": executor,
        "elapsed_seconds": elapsed_seconds,
        "totals": {
            "segments": sum(item.segments for item in values),
            "objects": sum(item.objects for item in values),
            "paragraphs": sum(item.paragraphs for item in values),
            "lists": sum(item.lists for item in values),
            "tables": sum(item.tables for item in values),
            "unknown": sum(item.unknown for item in values),
        },
        "items": [asdict(item) for item in values],
    }
    (corpus_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    columns = tuple(CorpusItem.__dataclass_fields__)
    tsv_rows = ["\t".join(columns)]
    tsv_rows.extend(
        "\t".join(
            str(getattr(item, column)).replace("\t", " ").replace("\n", " ")
            for column in columns
        )
        for item in values
    )
    (corpus_dir / "summary.tsv").write_text(
        "\n".join(tsv_rows) + "\n", encoding="utf-8"
    )
    markdown = [
        "# Stage 6 object reconstruction corpus",
        "",
        f"Status: **{summary['status']}**",
        "",
        (
            f"Images: {len(values)}; complete: {complete}; degraded: {degraded}; "
            f"failures: {failures}; exact partitions: {exact}."
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
        description="Run geometry stage 1 and image-free object stage 6 on a raster corpus"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "object-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--executor", choices=("process", "thread"), default="process")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fail-on-degraded", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("workers must be positive")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")
    input_root = args.input.resolve()
    sources = _discover(input_root)
    if args.limit is not None:
        sources = sources[: args.limit]
    if not sources:
        raise FileNotFoundError(f"no raster images at {input_root}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.run_id):
        raise ValueError("run-id contains unsafe characters")
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    corpus_dir = output_root / args.run_id
    corpus_dir.mkdir(exist_ok=False)
    started = time.perf_counter()
    if args.executor == "process":
        executor_factory = concurrent.futures.ProcessPoolExecutor
        executor_options: dict[str, object] = {
            "max_workers": args.workers,
            "mp_context": multiprocessing.get_context("spawn"),
        }
    else:
        executor_factory = concurrent.futures.ThreadPoolExecutor
        executor_options = {"max_workers": args.workers}
    with executor_factory(**executor_options) as executor:
        futures = tuple(
            executor.submit(
                _run_item,
                str(source),
                str(input_root),
                str(corpus_dir),
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
        executor=args.executor,
        elapsed_seconds=time.perf_counter() - started,
    )
    print(corpus_dir)
    print(
        f"images={summary['images']} complete={summary['complete']} "
        f"degraded={summary['degraded']} failures={summary['failures']} "
        f"exact={summary['exact_partitions']} workers={args.workers}"
    )
    if summary["failures"]:
        return 1
    if summary["degraded"] and args.fail_on_degraded:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
