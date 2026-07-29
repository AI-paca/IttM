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
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "ocr"))

from app.sparse_pipeline.geometry import GeometryAnalyzer  # noqa: E402
from app.sparse_pipeline.geometry_artifacts import GeometryArtifactWriter  # noqa: E402

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"})
PDF_STACK_MARKER = ".pdf.raster."
LINE_OWNER_SUFFIX = ".line-owner.mask.png"
LINE_OWNER_FRAME = int(np.iinfo(np.uint16).max)


@dataclass(frozen=True)
class CorpusItem:
    source: str
    status: str
    geometry_status: str
    elapsed_seconds: float
    foreground_pixels: int = 0
    segments: int = 0
    rules: int = 0
    nodes: int = 0
    cells: int = 0
    limit_leaf_count: int = 0
    expected_pixels: int | None = None
    lost_pixels: int | None = None
    added_pixels: int | None = None
    xor_pixels: int | None = None
    known_lines: int = 0
    coarse_line_delta: int = 0
    line_oracle_status: str = "not_applicable"
    line_oracle_lines: int | None = None
    text_owner_lost_pixels: int | None = None
    text_owner_added_pixels: int | None = None
    rule_owner_lost_pixels: int | None = None
    rule_owner_added_pixels: int | None = None
    cross_line_segments: int | None = None
    error: str = ""


@dataclass(frozen=True)
class LineOracleDelta:
    status: str = "not_applicable"
    line_count: int | None = None
    text_lost: int | None = None
    text_added: int | None = None
    rule_lost: int | None = None
    rule_added: int | None = None
    cross_line_segments: int | None = None
    error: str = ""


def safe_run_id(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-.")[:48] or "image"
    return f"{digest}-{stem}"


def _source_label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _stack_source_pdf(source: Path) -> Path | None:
    lowered = source.name.lower()
    marker_index = lowered.find(PDF_STACK_MARKER)
    if marker_index < 0:
        return None
    candidate = source.with_name(source.name[: marker_index + len(".pdf")])
    return candidate if candidate.is_file() else None


def _stack_pages(pages: tuple[Image.Image, ...], gap: int) -> Image.Image:
    if not pages:
        raise ValueError("PDF stack expansion rendered no pages")
    width = max(page.width for page in pages)
    height = sum(page.height for page in pages) + gap * (len(pages) - 1)
    stacked = Image.new("RGB", (width, height), "white")
    offset = 0
    for page in pages:
        stacked.paste(page, ((width - page.width) // 2, offset))
        offset += page.height + gap
    return stacked


def _render_pdf_pages(
    pdf_source: Path, *, dpi: int, max_pages: int
) -> tuple[Image.Image, ...]:
    from pdf2image import convert_from_path

    rendered = convert_from_path(
        str(pdf_source),
        dpi=dpi,
        first_page=1,
        last_page=max_pages,
        fmt="png",
        thread_count=1,
    )
    try:
        return tuple(page.convert("RGB") for page in rendered)
    finally:
        for page in rendered:
            page.close()


def _candidate_stack_dpis(
    pdf_source: Path, source_width: int, max_pages: int
) -> tuple[int, ...]:
    probe_dpi = 72
    pages = _render_pdf_pages(pdf_source, dpi=probe_dpi, max_pages=max_pages)
    try:
        probe_width = max(page.width for page in pages)
    finally:
        for page in pages:
            page.close()
    estimate = source_width * probe_dpi / probe_width
    rounded = round(estimate)
    return tuple(
        candidate
        for candidate in dict.fromkeys(
            (
                rounded,
                round(estimate - 1),
                round(estimate + 1),
                rounded - 2,
                rounded + 2,
            )
        )
        if candidate > 0
    )


def _expand_pdf_stack(
    source: Path,
    root: Path,
    staging: Path,
    *,
    dpi: int,
    max_pages: int,
    gap: int,
) -> tuple[tuple[Path, ...], dict[str, object]]:
    pdf_source = _stack_source_pdf(source)
    if pdf_source is None:
        return (source,), {"source": _source_label(source, root), "expanded": False}

    with Image.open(source) as opened:
        actual = opened.convert("RGB")
    candidates = (
        (dpi,)
        if dpi > 0
        else _candidate_stack_dpis(pdf_source, actual.width, max_pages)
    )
    pages: tuple[Image.Image, ...] = ()
    effective_dpi = 0
    try:
        for candidate in candidates:
            candidate_pages = _render_pdf_pages(
                pdf_source, dpi=candidate, max_pages=max_pages
            )
            stacked = _stack_pages(candidate_pages, gap)
            try:
                exact = (
                    actual.size == stacked.size
                    and ImageChops.difference(actual, stacked).getbbox() is None
                )
            finally:
                stacked.close()
            if exact:
                pages = candidate_pages
                effective_dpi = candidate
                break
            for page in candidate_pages:
                page.close()
        if not pages:
            rendered_dpis = ", ".join(str(candidate) for candidate in candidates)
            raise ValueError(
                f"generated PDF stack no longer matches dpi candidates [{rendered_dpis}], "
                f"max_pages={max_pages}, gap={gap}: {source}"
            )

        base = safe_run_id(source, root)
        outputs: list[Path] = []
        for page_number, page in enumerate(pages, start=1):
            output = staging / f"{base}.page-{page_number:03d}.png"
            page.save(output, format="PNG")
            outputs.append(output)
        return tuple(outputs), {
            "source": _source_label(source, root),
            "pdf": _source_label(pdf_source, root),
            "expanded": True,
            "exact_stack_match": True,
            "pages": len(outputs),
            "dpi": effective_dpi,
            "gap": gap,
            "outputs": [path.name for path in outputs],
        }
    finally:
        actual.close()
        for page in pages:
            page.close()


def _prepare_sources(
    sources: tuple[Path, ...],
    root: Path,
    corpus_dir: Path,
    *,
    workers: int,
    expand_pdf_stacks: bool,
    dpi: int,
    max_pages: int,
    gap: int,
) -> tuple[Path, ...]:
    if not expand_pdf_stacks:
        return sources
    staging = corpus_dir / "page-inputs"
    staging.mkdir()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = tuple(
            executor.submit(
                _expand_pdf_stack,
                source,
                root,
                staging,
                dpi=dpi,
                max_pages=max_pages,
                gap=gap,
            )
            for source in sources
        )
        expanded = tuple(future.result() for future in futures)
    prepared = tuple(path for paths, _ in expanded for path in paths)
    manifest = {
        "dpi": dpi,
        "max_pages": max_pages,
        "gap": gap,
        "input_images": len(sources),
        "page_units": len(prepared),
        "items": [record for _, record in expanded],
    }
    (corpus_dir / "page-expansion.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return prepared


def _known_line_count(reference: Path) -> int:
    if not reference.is_file():
        return 0
    return sum(
        bool(line.strip())
        for line in reference.read_text(encoding="utf-8").splitlines()
    )


def _mask_delta(
    expected_path: Path,
    actual: np.ndarray,
    *,
    require_exact_mask: bool,
) -> tuple[int | None, int | None, int | None, int | None, str]:
    if not expected_path.is_file():
        error = (
            f"required sibling mask is missing: {expected_path.name}"
            if require_exact_mask
            else ""
        )
        return None, None, None, None, error
    with Image.open(expected_path) as opened:
        expected = np.asarray(opened.convert("L"), dtype=np.uint8) > 0
    expected_pixels = int(np.count_nonzero(expected))
    actual_mask = np.asarray(actual, dtype=bool)
    if expected.shape != actual_mask.shape:
        error = (
            f"mask shape mismatch: expected={expected.shape} actual={actual_mask.shape}"
        )
        return expected_pixels, None, None, None, error
    lost = int(np.count_nonzero(expected & ~actual_mask))
    added = int(np.count_nonzero(actual_mask & ~expected))
    return expected_pixels, lost, added, lost + added, ""


def _line_oracle_delta(
    expected_path: Path,
    ownership: np.ndarray,
    rule_mask: np.ndarray,
    *,
    require_line_oracle: bool,
    applicable: bool = True,
) -> LineOracleDelta:
    if not expected_path.is_file():
        if not applicable:
            return LineOracleDelta(status="not_applicable")
        error = (
            f"required sibling line oracle is missing: {expected_path.name}"
            if require_line_oracle
            else ""
        )
        return LineOracleDelta(
            status="missing" if require_line_oracle else "unavailable",
            error=error,
        )
    with Image.open(expected_path) as opened:
        expected = np.asarray(opened, dtype=np.uint16)
    if expected.ndim != 2:
        return LineOracleDelta(
            status="invalid",
            error=f"line oracle must be a 2-D uint16 PNG: {expected_path.name}",
        )
    actual_ownership = np.asarray(ownership)
    actual_rules = np.asarray(rule_mask, dtype=bool)
    if expected.shape != actual_ownership.shape or expected.shape != actual_rules.shape:
        return LineOracleDelta(
            status="invalid",
            error=(
                f"line oracle shape mismatch: expected={expected.shape} "
                f"ownership={actual_ownership.shape} rules={actual_rules.shape}"
            ),
        )
    labels = np.unique(expected)
    line_labels = labels[(labels > 0) & (labels < LINE_OWNER_FRAME)]
    canonical_labels = np.arange(1, len(line_labels) + 1, dtype=np.uint16)
    if not np.array_equal(line_labels, canonical_labels):
        return LineOracleDelta(
            status="invalid",
            error=f"line oracle labels must be contiguous from one: {line_labels.tolist()}",
        )

    expected_text = np.logical_and(expected > 0, expected < LINE_OWNER_FRAME)
    expected_rules = expected == LINE_OWNER_FRAME
    actual_text = actual_ownership >= 0
    text_lost = int(np.count_nonzero(expected_text & ~actual_text))
    text_added = int(np.count_nonzero(actual_text & ~expected_text))
    rule_lost = int(np.count_nonzero(expected_rules & ~actual_rules))
    rule_added = int(np.count_nonzero(actual_rules & ~expected_rules))
    crossings = 0
    for segment_id in np.unique(actual_ownership[actual_text]):
        segment_labels = np.unique(expected[actual_ownership == segment_id])
        segment_lines = segment_labels[
            (segment_labels > 0) & (segment_labels < LINE_OWNER_FRAME)
        ]
        crossings += len(segment_lines) > 1
    return LineOracleDelta(
        status="exact",
        line_count=len(line_labels),
        text_lost=text_lost,
        text_added=text_added,
        rule_lost=rule_lost,
        rule_added=rule_added,
        cross_line_segments=crossings,
    )


def run_item(
    source_value: str,
    root_value: str,
    output_value: str,
    require_exact_mask: bool = False,
    require_line_oracle: bool = False,
) -> CorpusItem:
    source = Path(source_value)
    root = Path(root_value)
    output = Path(output_value)
    started = time.perf_counter()
    try:
        with Image.open(source) as opened:
            opened.load()
            bundle = GeometryAnalyzer().analyze_bundle(opened)
        GeometryArtifactWriter().write(
            output, run_id=safe_run_id(source, root), bundle=bundle
        )
        result = bundle.result
        expected_pixels, lost_pixels, added_pixels, xor_pixels, mask_error = (
            _mask_delta(
                source.with_suffix(".mask.png"),
                bundle.foreground_mask,
                require_exact_mask=require_exact_mask,
            )
        )
        reference_path = source.with_suffix(".txt")
        known_lines = _known_line_count(reference_path)
        coarse_line_delta = max(0, known_lines - len(result.segmentation.segments))
        line_delta = _line_oracle_delta(
            source.with_suffix(LINE_OWNER_SUFFIX),
            bundle.ownership,
            bundle.rule_mask,
            require_line_oracle=require_line_oracle,
            applicable=reference_path.is_file(),
        )
        reference_mismatch = bool(
            reference_path.is_file()
            and line_delta.line_count is not None
            and known_lines != line_delta.line_count
        )
        line_discrepancy = any(
            value not in (None, 0)
            for value in (
                line_delta.text_lost,
                line_delta.text_added,
                line_delta.rule_lost,
                line_delta.rule_added,
                line_delta.cross_line_segments,
            )
        )
        line_oracle_status = (
            "mismatch" if line_discrepancy or reference_mismatch else line_delta.status
        )
        geometry_status = result.status.value
        if (
            mask_error
            or (xor_pixels is not None and xor_pixels != 0)
            or line_delta.error
            or line_discrepancy
            or reference_mismatch
        ):
            status = "failed"
        elif (
            geometry_status == "degraded"
            or coarse_line_delta
            or line_oracle_status == "unavailable"
        ):
            status = "degraded"
        else:
            status = "complete"
        errors = []
        if mask_error:
            errors.append(mask_error)
        if xor_pixels:
            errors.append(
                f"foreground mask differs: lost={lost_pixels} added={added_pixels} xor={xor_pixels}"
            )
        if coarse_line_delta:
            errors.append(
                f"coarse segment deficit: known_lines={known_lines} segments={len(result.segmentation.segments)}"
            )
        if line_delta.error:
            errors.append(line_delta.error)
        elif line_oracle_status == "unavailable":
            errors.append(
                f"line oracle unavailable: {source.with_suffix(LINE_OWNER_SUFFIX).name}"
            )
        if line_discrepancy:
            errors.append(
                "line owner differs: "
                f"text_lost={line_delta.text_lost} text_added={line_delta.text_added} "
                f"rule_lost={line_delta.rule_lost} rule_added={line_delta.rule_added} "
                f"cross_line_segments={line_delta.cross_line_segments}"
            )
        if reference_mismatch:
            errors.append(
                f"reference/oracle line mismatch: reference={known_lines} oracle={line_delta.line_count}"
            )
        return CorpusItem(
            source=_source_label(source, root),
            status=status,
            geometry_status=geometry_status,
            elapsed_seconds=time.perf_counter() - started,
            foreground_pixels=result.alignment.foreground_pixels,
            segments=len(result.segmentation.segments),
            rules=len(result.segmentation.rules),
            nodes=len(result.segmentation.nodes),
            cells=len(result.matrix.cells),
            limit_leaf_count=result.limit_leaf_count,
            expected_pixels=expected_pixels,
            lost_pixels=lost_pixels,
            added_pixels=added_pixels,
            xor_pixels=xor_pixels,
            known_lines=known_lines,
            coarse_line_delta=coarse_line_delta,
            line_oracle_status=line_oracle_status,
            line_oracle_lines=line_delta.line_count,
            text_owner_lost_pixels=line_delta.text_lost,
            text_owner_added_pixels=line_delta.text_added,
            rule_owner_lost_pixels=line_delta.rule_lost,
            rule_owner_added_pixels=line_delta.rule_added,
            cross_line_segments=line_delta.cross_line_segments,
            error="; ".join(errors),
        )
    except Exception as exc:
        return CorpusItem(
            source=_source_label(source, root),
            status="failed",
            geometry_status="failed",
            elapsed_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parallel queued replay of stage 1 over raster debug files"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "debug" / "tmp" / "geometry-corpus",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--executor",
        choices=("process", "thread"),
        default="process",
        help="process is the CPU-parallel default; thread remains available for restricted CI sandboxes",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--expand-pdf-stacks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="replace generated *.pdf.raster images with exact page units before the queued run",
    )
    parser.add_argument(
        "--pdf-dpi", type=int, default=0, help="zero infers the exact stack render DPI"
    )
    parser.add_argument("--pdf-max-pages", type=int, default=5)
    parser.add_argument("--pdf-stack-gap", type=int, default=32)
    parser.add_argument(
        "--require-exact-masks",
        action="store_true",
        help="fail an item when its sibling .mask.png is missing or differs by any pixel",
    )
    parser.add_argument(
        "--require-exact-line-oracles",
        "--require-line-oracles",
        dest="require_exact_line_oracles",
        action="store_true",
        help="fail generated items when their sibling .line-owner.mask.png is missing or differs",
    )
    parser.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="return exit code 2 when no item failed but one or more items are degraded",
    )
    return parser.parse_args()


def _markdown_value(value: object) -> str:
    if value is None:
        return "\u2014"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _write_markdown(
    corpus_dir: Path, summary: dict[str, object], values: tuple[CorpusItem, ...]
) -> None:
    columns = tuple(CorpusItem.__dataclass_fields__)
    lines = [
        "# Stage 1 geometry corpus",
        "",
        f"Status: **{summary['status']}**",
        "",
        (
            f"Images: {summary['images']}; complete: {summary['complete']}; "
            f"degraded: {summary['degraded']}; failures: {summary['failures']}; "
            f"workers: {summary['workers']}."
        ),
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| "
        + " | ".join(_markdown_value(getattr(item, column)) for column in columns)
        + " |"
        for item in values
    )
    (corpus_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("workers must be positive")
    input_root = args.input.resolve()
    sources = tuple(
        path
        for path in sorted(input_root.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in IMAGE_SUFFIXES
        and not path.name.lower().endswith((".mask.png", LINE_OWNER_SUFFIX))
    )
    if args.limit is not None:
        sources = sources[: args.limit]
    if not sources:
        raise FileNotFoundError(f"no raster images below {input_root}")
    corpus_dir = args.output.resolve() / args.run_id
    corpus_dir.mkdir(parents=True, exist_ok=False)
    sources = _prepare_sources(
        sources,
        input_root,
        corpus_dir,
        workers=args.workers,
        expand_pdf_stacks=getattr(args, "expand_pdf_stacks", True),
        dpi=getattr(args, "pdf_dpi", 0),
        max_pages=getattr(args, "pdf_max_pages", 5),
        gap=getattr(args, "pdf_stack_gap", 32),
    )
    started = time.perf_counter()
    executor_kind = getattr(args, "executor", "process")
    if executor_kind == "process":
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
                run_item,
                str(source),
                str(input_root),
                str(corpus_dir),
                args.require_exact_masks,
                getattr(args, "require_exact_line_oracles", False),
            )
            for source in sources
        )
        values = tuple(
            future.result() for future in concurrent.futures.as_completed(futures)
        )
    values = tuple(sorted(values, key=lambda item: item.source))
    failures = sum(item.status == "failed" for item in values)
    degraded = sum(item.status == "degraded" for item in values)
    complete = sum(item.status == "complete" for item in values)
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
    summary = {
        "status": "failed" if failures else "degraded" if degraded else "complete",
        "images": len(values),
        "complete": complete,
        "degraded": degraded,
        "failures": failures,
        "workers": args.workers,
        "executor": executor_kind,
        "page_units": len(values),
        "require_exact_masks": args.require_exact_masks,
        "require_exact_line_oracles": getattr(
            args, "require_exact_line_oracles", False
        ),
        "fail_on_degraded": args.fail_on_degraded,
        "elapsed_seconds": time.perf_counter() - started,
        "items": [item.__dict__ for item in values],
    }
    (corpus_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_markdown(corpus_dir, summary, values)
    print(corpus_dir)
    print(
        f"images={len(values)} complete={complete} degraded={degraded} "
        f"failures={failures} workers={args.workers}"
    )
    if failures:
        return 1
    if degraded and args.fail_on_degraded:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
