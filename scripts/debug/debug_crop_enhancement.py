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

import numpy as np
from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.crop_enhancement import (  # noqa: E402
    CANDIDATE_ROLE,
    CropEnhancementConfig,
    CropInput,
    EnhancementBackend,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.enhancement_artifacts import (  # noqa: E402
    CropEnhancementArtifactWriter,
)

IGNORED_SUFFIXES = ("source.png", "enhanced.png", "-overlay.png", ".mask.png")


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    elapsed_seconds: float
    width: int = 0
    height: int = 0
    source_bytes: int = 0
    output_bytes: int = 0
    backend: str = ""
    deterministic: bool = False
    same_geometry: bool = False
    black_pixels_before: int = 0
    black_pixels_after: int = 0
    white_pixels_before: int = 0
    white_pixels_after: int = 0
    binary_extremes_preserved: bool = False
    artifact: str = ""
    error: str = ""


def _safe_id(path: Path, root: Path) -> str:
    relative = path.name if root.is_file() else path.relative_to(root).as_posix()
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-.")[:40]
    return f"{digest}-{stem or 'crop'}"


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
        if path.is_file() and path.suffix.lower() == ".png" and not path.name.lower().endswith(IGNORED_SUFFIXES)
    )


def _flatten_rgb(payload: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(payload)) as opened:
        opened.load()
        if opened.mode in {"RGBA", "LA"} or "transparency" in opened.info:
            rgba = opened.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            try:
                canvas.alpha_composite(rgba)
                rgb = canvas.convert("RGB")
            finally:
                rgba.close()
                canvas.close()
        else:
            rgb = opened.convert("RGB")
        try:
            return np.array(rgb, dtype=np.uint8, copy=True)
        finally:
            rgb.close()


def _output_pixels(payload: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(payload)) as opened:
        opened.load()
        return np.array(opened, dtype=np.uint8, copy=True)


def _run_item(
    source_text: str,
    input_text: str,
    corpus_text: str,
    backend_text: str,
) -> CorpusItem:
    source = Path(source_text)
    input_root = Path(input_text)
    corpus_dir = Path(corpus_text)
    label = _label(source, input_root)
    started = time.perf_counter()
    try:
        payload = source.read_bytes()
        crop_id = _safe_id(source, input_root)
        item = CropInput(crop_id, payload)
        enhancer = GammaDarkCropEnhancer(CropEnhancementConfig(backend=EnhancementBackend(backend_text)))
        first = enhancer.enhance(item)
        second = enhancer.enhance(item)
        source_pixels = _flatten_rgb(payload)
        output_pixels = _output_pixels(first.png_bytes)
        source_black = np.all(source_pixels == 0, axis=2)
        source_white = np.all(source_pixels == 255, axis=2)
        black_after = int(np.count_nonzero(output_pixels[source_black] == 0))
        white_after = int(np.count_nonzero(output_pixels[source_white] == 255))
        black_before = int(np.count_nonzero(source_black))
        white_before = int(np.count_nonzero(source_white))
        same_geometry = output_pixels.shape == source_pixels.shape[:2]
        deterministic = first.png_bytes == second.png_bytes
        extremes_preserved = black_after == black_before and white_after == white_before
        if not (same_geometry and deterministic and extremes_preserved):
            raise RuntimeError("stage 4 invariant failed")
        artifact = CropEnhancementArtifactWriter().write(
            corpus_dir / "items",
            run_id=crop_id,
            inputs=(item,),
            results=(first,),
        )
        return CorpusItem(
            source=label,
            status="complete",
            elapsed_seconds=time.perf_counter() - started,
            width=first.width,
            height=first.height,
            source_bytes=len(payload),
            output_bytes=len(first.png_bytes),
            backend=first.backend.value,
            deterministic=deterministic,
            same_geometry=same_geometry,
            black_pixels_before=black_before,
            black_pixels_after=black_after,
            white_pixels_before=white_before,
            white_pixels_after=white_after,
            binary_extremes_preserved=extremes_preserved,
            artifact=artifact.relative_to(corpus_dir).as_posix(),
        )
    except Exception as exc:  # preserve every corpus failure in the report
        return CorpusItem(
            source=label,
            status="failed",
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = tuple(sorted(values))
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def _write_summaries(
    corpus_dir: Path,
    *,
    values: tuple[CorpusItem, ...],
    workers: int,
    backend: str,
    elapsed_seconds: float,
) -> dict[str, object]:
    failures = sum(item.status == "failed" for item in values)
    durations = tuple(item.elapsed_seconds for item in values)
    summary: dict[str, object] = {
        "semantic_stage": 4,
        "execution_step": 4,
        "status": "failed" if failures else "complete",
        "images": len(values),
        "complete": len(values) - failures,
        "failures": failures,
        "workers": workers,
        "executor": "process",
        "requested_backend": backend,
        "candidate_role": CANDIDATE_ROLE,
        "selection_stage": 2,
        "unconditional_default": False,
        "elapsed_seconds": elapsed_seconds,
        "item_seconds_mean": statistics.fmean(durations) if durations else 0.0,
        "item_seconds_p50": _percentile(durations, 0.50),
        "item_seconds_p95": _percentile(durations, 0.95),
        "deterministic": sum(item.deterministic for item in values),
        "same_geometry": sum(item.same_geometry for item in values),
        "binary_extremes_preserved": sum(item.binary_extremes_preserved for item in values),
        "source_bytes": sum(item.source_bytes for item in values),
        "output_bytes": sum(item.output_bytes for item in values),
        "items": [asdict(item) for item in values],
    }
    (corpus_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    columns = tuple(CorpusItem.__dataclass_fields__)
    rows = ["\t".join(columns)]
    rows.extend(
        "\t".join(str(getattr(item, column)).replace("\t", " ").replace("\n", " ") for column in columns)
        for item in values
    )
    (corpus_dir / "summary.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    markdown = [
        "# Stage 4 crop enhancement corpus",
        "",
        f"Status: **{summary['status']}**",
        "",
        (f"Images: {len(values)}; complete: {summary['complete']}; failures: {failures}; workers: {workers}."),
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    markdown.extend(
        "| "
        + " | ".join(str(getattr(item, column)).replace("|", "\\|").replace("\n", "<br>") for column in columns)
        + " |"
        for item in values
    )
    (corpus_dir / "summary.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic stage 4 enhancement on a PNG crop corpus")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "enhancement-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--backend",
        choices=tuple(item.value for item in EnhancementBackend),
        default=EnhancementBackend.AUTO.value,
    )
    parser.add_argument("--limit", type=int)
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
                args.backend,
            )
            for source in sources
        )
        values = tuple(
            sorted(
                (future.result() for future in concurrent.futures.as_completed(futures)),
                key=lambda item: item.source,
            )
        )
    summary = _write_summaries(
        corpus_dir,
        values=values,
        workers=args.workers,
        backend=args.backend,
        elapsed_seconds=time.perf_counter() - started,
    )
    print(corpus_dir)
    print(
        f"images={summary['images']} complete={summary['complete']} "
        f"failures={summary['failures']} workers={args.workers}"
    )
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
