#!/usr/bin/env python3
"""Build a browsable SVG report from separated-pipeline OCR block artifacts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import csv
import html
import io
import json
import math
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


def _embedded_png_href(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _embedded_thumbnail_href(
    path: Path, *, max_width: int, max_height: int
) -> str:
    with Image.open(path) as source:
        image = source.copy()
    image.thumbnail(
        (max_width, max_height),
        resample=Image.Resampling.LANCZOS,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


STAGE_DIRECTORIES = {
    "preprocess": "00-preprocess",
    "geometry": "01-geometry",
    "topology": "02-topology",
    "find-object": "03-find-object",
    "separate-block": "04-separate-block",
    "ocr-blocks": "05-ocr-blocks",
    "get-segment": "06-get-segment",
    "generate-object": "07-generate-object",
}

PAGE_WIDTH = 1800
PAGE_PADDING = 34
BLOCKS_PER_PAGE = 8
RAW_X = 42
GAMMA_X = 920
BLOCK_MAX_WIDTH = 820
BLOCK_MAX_HEIGHT = 560
TEXT_LINE_HEIGHT = 22


@dataclass(frozen=True)
class BlockRecord:
    object_stem: str
    object_kind: str
    policy: str
    block_id: str
    index: int
    segment_ids: tuple[str, ...]
    core_segment_ids: tuple[str, ...]
    context_segment_ids: tuple[str, ...]
    masked_segment_ids: tuple[str, ...]
    bbox: tuple[int, int, int, int]
    object_bbox: tuple[int, int, int, int]
    raw_path: Path
    gamma_path: Path
    raw_job: dict[str, Any] | None
    gamma_job: dict[str, Any] | None


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _bbox(value: object) -> tuple[int, int, int, int]:
    if (
        isinstance(value, list)
        and len(value) == 4
        and all(type(item) is int for item in value)
    ):
        return tuple(value)  # type: ignore[return-value]
    if isinstance(value, dict):
        keys = ("left", "top", "right", "bottom")
        if all(type(value.get(key)) is int for key in keys):
            return tuple(int(value[key]) for key in keys)  # type: ignore[return-value]
    raise ValueError(f"invalid bbox: {value!r}")


def _read_segments(path: Path) -> dict[str, tuple[int, int, int, int]]:
    result: dict[str, tuple[int, int, int, int]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = json.loads(raw_line)
        segment_id = payload.get("segment_id")
        if isinstance(segment_id, str):
            result[segment_id] = _bbox(payload.get("bbox"))
    return result


def _read_items(path: Path) -> list[tuple[str, Path]]:
    records: list[tuple[str, Path]] = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        for row in reader:
            records.append((row["item_id"], Path(row["source"])))
    return records


def _iter_report_items(run_dir: Path) -> list[tuple[str, Path, Path, Path]]:
    items_path = run_dir / "items.tsv"
    if items_path.is_file():
        return [
            (
                item_id,
                source,
                run_dir / "items" / item_id,
                run_dir
                / "items"
                / item_id
                / STAGE_DIRECTORIES["geometry"]
                / "segments.jsonl",
            )
            for item_id, source in _read_items(items_path)
        ]

    raster = run_dir / STAGE_DIRECTORIES["preprocess"] / "raster.png"
    if raster.is_file():
        geometry = (
            run_dir / STAGE_DIRECTORIES["geometry"] / "segments.jsonl"
        )
        return [(run_dir.name, raster, run_dir, geometry)]

    source_manifest = (
        run_dir
        / STAGE_DIRECTORIES["find-object"]
        / "source-manifest.json"
    )
    if source_manifest.is_file():
        payload = json.loads(source_manifest.read_text(encoding="utf-8"))
        inputs = payload.get("inputs", {})
        image = Path(inputs["image"])
        geometry = Path(inputs["segments"])
        return [(run_dir.name, image, run_dir, geometry)]

    raise FileNotFoundError(
        f"{run_dir} is neither a batch run with items.tsv nor a single stage run"
    )


def _runner_status(item_dir: Path, stage: str) -> str:
    path = item_dir / STAGE_DIRECTORIES[stage] / ".runner-status"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().upper()
    manifest_path = item_dir / STAGE_DIRECTORIES[stage] / "manifest.json"
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(payload.get("status", "--")).upper()
    return "--"


def _relative_href(source: Path, svg_path: Path) -> str:
    if not source.is_file():
        raise FileNotFoundError(source)
    payload = source.read_bytes()
    report_root = svg_path.parents[2]
    assets = report_root / "assets"
    assets.mkdir(exist_ok=True)
    suffix = source.suffix.lower() or ".bin"
    target = assets / f"{hashlib.sha256(payload).hexdigest()}{suffix}"
    if not target.is_file():
        target.write_bytes(payload)
    return Path(os.path.relpath(target, svg_path.parent)).as_posix()


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as opened:
        return opened.size


def _scaled_size(
    path: Path,
    *,
    max_width: int,
    max_height: int,
    min_width: int = 0,
) -> tuple[float, float, float]:
    width, height = _image_size(path)
    scale = min(max_width / width, max_height / height)
    if min_width:
        scale = min(scale, max(1.0, min_width / width))
    return width * scale, height * scale, scale


def _job_text(job: dict[str, Any] | None) -> str:
    if job is None:
        return "<missing OCR job>"
    output = job.get("output")
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        return output["text"]
    error = job.get("error_message")
    return str(error) if error else "<empty OCR output>"


def _normalized_job_text(job: dict[str, Any] | None) -> str:
    return " ".join(_job_text(job).split())


def _mean_confidence(job: dict[str, Any] | None) -> float | None:
    if job is None:
        return None
    output = job.get("output")
    words = output.get("words") if isinstance(output, dict) else None
    if not isinstance(words, list):
        return None
    values = [
        float(word["confidence"])
        for word in words
        if isinstance(word, dict)
        and isinstance(word.get("confidence"), (int, float))
    ]
    return sum(values) / len(values) if values else None


def _wrapped_job_lines(
    transform: str,
    job: dict[str, Any] | None,
    *,
    width: int = 92,
) -> list[str]:
    status = str(job.get("status", "missing")) if job else "missing"
    confidence = _mean_confidence(job)
    confidence_text = "n/a" if confidence is None else f"{confidence:.3f}"
    lines = [f"{transform} | status={status} | mean confidence={confidence_text}"]
    for source_line in _job_text(job).splitlines() or [""]:
        wrapped = textwrap.wrap(
            source_line,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        lines.extend(wrapped or [""])
    return lines


def _words(job: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    if job is None:
        return ()
    output = job.get("output")
    words = output.get("words") if isinstance(output, dict) else None
    if not isinstance(words, list):
        return ()
    return (word for word in words if isinstance(word, dict))


def _segment_rectangles(
    block: BlockRecord,
    geometry: dict[str, tuple[int, int, int, int]],
) -> list[tuple[str, str, tuple[int, int, int, int]]]:
    object_left, object_top, _, _ = block.object_bbox
    block_left, block_top, block_right, block_bottom = block.bbox
    classes = {
        **{item: "core" for item in block.core_segment_ids},
        **{item: "context" for item in block.context_segment_ids},
        **{item: "masked" for item in block.masked_segment_ids},
    }
    result: list[tuple[str, str, tuple[int, int, int, int]]] = []
    for segment_id in block.segment_ids:
        page = geometry.get(segment_id)
        if page is None:
            continue
        local = (
            page[0] - object_left,
            page[1] - object_top,
            page[2] - object_left,
            page[3] - object_top,
        )
        clipped = (
            max(local[0], block_left) - block_left,
            max(local[1], block_top) - block_top,
            min(local[2], block_right) - block_left,
            min(local[3], block_bottom) - block_top,
        )
        if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
            result.append((segment_id, classes.get(segment_id, "owned"), clipped))
    return result


def _load_blocks(item_dir: Path) -> list[BlockRecord]:
    block_root = item_dir / STAGE_DIRECTORIES["separate-block"] / "objects"
    ocr_root = item_dir / STAGE_DIRECTORIES["ocr-blocks"] / "objects"
    object_root = item_dir / STAGE_DIRECTORIES["find-object"] / "objects"
    records: list[BlockRecord] = []
    for object_dir in sorted(path for path in block_root.iterdir() if path.is_dir()):
        object_stem = object_dir.name
        object_payload = _json(object_root / object_stem / "object.json")
        object_kind = str(object_payload.get("kind", "unknown"))
        object_bbox = _bbox(object_payload.get("bbox"))
        object_ocr_root = ocr_root / object_stem
        jobs_by_policy: dict[str, list[dict[str, Any]]] = {}
        if object_ocr_root.is_dir():
            for jobs_path in sorted(object_ocr_root.glob("*/jobs.json")):
                payload = _json(jobs_path)
                jobs_by_policy[jobs_path.parent.name] = [
                    job
                    for job in payload.get("jobs", [])
                    if isinstance(job, dict)
                ]
        block_policies: set[str] = set()
        for policy_dir in sorted(
            path for path in object_dir.iterdir() if path.is_dir()
        ):
            policy = policy_dir.name
            block_policies.add(policy)
            policy_jobs = jobs_by_policy.get(policy, [])
            for block_offset, block_path in enumerate(
                sorted(policy_dir.glob("block-*.json"))
            ):
                block = _json(block_path)
                stem = block_path.name.removesuffix(".json")
                raw_path = policy_dir / f"{stem}.raw.png"
                if not raw_path.is_file():
                    raise FileNotFoundError(
                        f"missing block raster for {block_path}"
                    )
                selected_job = (
                    policy_jobs[block_offset]
                    if block_offset < len(policy_jobs)
                    else None
                )
                block_id = str(block["block_id"])
                records.append(
                    BlockRecord(
                        object_stem=object_stem,
                        object_kind=object_kind,
                        policy=policy,
                        block_id=block_id,
                        index=int(block.get("index", block_offset + 1)),
                        segment_ids=tuple(block.get("segment_ids", ())),
                        core_segment_ids=tuple(
                            block.get("core_segment_ids", ())
                        ),
                        context_segment_ids=tuple(
                            block.get("context_segment_ids", ())
                        ),
                        masked_segment_ids=tuple(
                            block.get("masked_segment_ids", ())
                        ),
                        bbox=_bbox(block.get("bbox")),
                        object_bbox=object_bbox,
                        raw_path=raw_path,
                        gamma_path=raw_path,
                        raw_job=selected_job,
                        gamma_job=selected_job,
                    )
                )
        object_image = object_root / object_stem / f"{object_stem}.png"
        if not object_image.is_file():
            continue
        image_width, image_height = _image_size(object_image)
        object_segments = tuple(object_payload.get("segment_ids", ()))
        for policy, policy_jobs in sorted(jobs_by_policy.items()):
            if policy in block_policies:
                continue
            for job_offset, selected_job in enumerate(policy_jobs):
                job_block_id = str(
                    selected_job.get("block_id", f"block-{job_offset:06d}")
                )
                synthetic_policy = f"{policy}-ocr-fallback"
                records.append(
                    BlockRecord(
                        object_stem=object_stem,
                        object_kind=object_kind,
                        policy=synthetic_policy,
                        block_id=job_block_id,
                        index=job_offset + 1,
                        segment_ids=object_segments,
                        core_segment_ids=object_segments,
                        context_segment_ids=(),
                        masked_segment_ids=(),
                        bbox=(0, 0, image_width, image_height),
                        object_bbox=object_bbox,
                        raw_path=object_image,
                        gamma_path=object_image,
                        raw_job=selected_job,
                        gamma_job=selected_job,
                    )
                )
    return sorted(
        records,
        key=lambda item: (
            item.object_stem,
            item.policy,
            item.index,
            item.block_id,
        ),
    )


def _svg_text(
    parts: list[str],
    *,
    x: float,
    y: float,
    lines: Iterable[str],
    css_class: str,
    line_height: int = TEXT_LINE_HEIGHT,
) -> None:
    parts.append(
        f'<text x="{x:.1f}" y="{y:.1f}" class="{css_class}">'
    )
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else line_height
        parts.append(
            f'<tspan x="{x:.1f}" dy="{dy}">{html.escape(line)}</tspan>'
        )
    parts.append("</text>")


def _overlay(
    parts: list[str],
    *,
    block: BlockRecord,
    geometry: dict[str, tuple[int, int, int, int]],
    job: dict[str, Any] | None,
    x: float,
    y: float,
    scale: float,
) -> None:
    colors = {
        "core": "#16a34a",
        "context": "#f59e0b",
        "masked": "#64748b",
        "owned": "#ef4444",
    }
    for segment_id, kind, bbox in _segment_rectangles(block, geometry):
        left, top, right, bottom = bbox
        width = max(1.0, (right - left) * scale)
        height = max(1.0, (bottom - top) * scale)
        parts.append(
            f'<rect x="{x + left * scale:.1f}" y="{y + top * scale:.1f}" '
            f'width="{width:.1f}" height="{height:.1f}" '
            f'fill="none" stroke="{colors[kind]}" stroke-width="2.5"/>'
        )
        if width >= 42 and height >= 18:
            parts.append(
                f'<text x="{x + left * scale + 3:.1f}" '
                f'y="{y + top * scale + 15:.1f}" class="segment-label">'
                f'{html.escape(segment_id.removeprefix("segment-"))}</text>'
            )
    for word in _words(job):
        try:
            left, top, right, bottom = _bbox(word.get("bbox"))
        except ValueError:
            continue
        parts.append(
            f'<rect x="{x + left * scale:.1f}" y="{y + top * scale:.1f}" '
            f'width="{max(1.0, (right - left) * scale):.1f}" '
            f'height="{max(1.0, (bottom - top) * scale):.1f}" '
            'fill="none" stroke="#0284c7" stroke-width="1.5" '
            'stroke-dasharray="6 4" opacity="0.85"/>'
        )


def _write_page(
    path: Path,
    *,
    title: str,
    page_number: int,
    page_count: int,
    source_path: Path,
    contact_sheet: Path | None,
    blocks: list[BlockRecord],
    geometry: dict[str, tuple[int, int, int, int]],
) -> tuple[int, int]:
    source_width, source_height, _ = _scaled_size(
        source_path, max_width=620, max_height=420
    )
    contact_width = 0.0
    contact_height = 0.0
    if contact_sheet is not None and contact_sheet.is_file():
        contact_width, contact_height, _ = _scaled_size(
            contact_sheet, max_width=980, max_height=420
        )
    overview_height = max(source_height, contact_height)
    y = 126 + overview_height + 76
    card_layouts: list[dict[str, Any]] = []
    conflicts = 0
    failed_jobs = 0
    for block in blocks:
        raw_width, raw_height, raw_scale = _scaled_size(
            block.raw_path,
            max_width=1700,
            max_height=1200,
            min_width=900,
        )
        raw_lines = _wrapped_job_lines(
            "selected adaptive OCR", block.raw_job, width=180
        )
        text_height = len(raw_lines) * TEXT_LINE_HEIGHT
        card_height = 118 + raw_height + text_height + 58
        missing_job = (
            block.raw_job is None
            or block.raw_job.get("status") != "complete"
        )
        failed_jobs += int(missing_job)
        card_layouts.append(
            {
                "block": block,
                "y": y,
                "height": card_height,
                "raw_size": (raw_width, raw_height, raw_scale),
                "raw_lines": raw_lines,
                "conflict": False,
            }
        )
        y += card_height + 26
    svg_height = int(math.ceil(y + 30))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{PAGE_WIDTH}" height="{svg_height}" '
            f'viewBox="0 0 {PAGE_WIDTH} {svg_height}">'
        ),
        "<style>",
        "text { font-family: 'DejaVu Sans', sans-serif; fill: #172033; }",
        ".title { font-size: 30px; font-weight: 700; }",
        ".subtitle { font-size: 18px; fill: #475569; }",
        ".block-title { font-size: 20px; font-weight: 700; }",
        ".meta { font-size: 15px; fill: #475569; }",
        ".ocr { font-family: 'DejaVu Sans Mono', monospace; font-size: 16px; }",
        ".segment-label { font-family: monospace; font-size: 12px; font-weight: 700; fill: #111827; paint-order: stroke; stroke: white; stroke-width: 3px; }",
        ".legend { font-size: 14px; fill: #334155; }",
        "</style>",
        f'<rect width="{PAGE_WIDTH}" height="{svg_height}" fill="#f5f1e8"/>',
        f'<text x="{PAGE_PADDING}" y="45" class="title">{html.escape(title)}</text>',
        (
            f'<text x="{PAGE_PADDING}" y="76" class="subtitle">'
            f'page {page_number}/{page_count} | blocks {len(blocks)} | '
            "solid: source segments | dashed blue: OCR words</text>"
        ),
        '<rect x="34" y="92" width="14" height="14" fill="none" stroke="#16a34a" stroke-width="3"/>',
        '<text x="56" y="104" class="legend">core</text>',
        '<rect x="116" y="92" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="3"/>',
        '<text x="138" y="104" class="legend">context</text>',
        '<rect x="230" y="92" width="14" height="14" fill="none" stroke="#0284c7" stroke-width="2" stroke-dasharray="5 3"/>',
        '<text x="252" y="104" class="legend">OCR word</text>',
        f'<text x="{RAW_X}" y="122" class="meta">source raster</text>',
        (
            f'<image x="{RAW_X}" y="132" width="{source_width:.1f}" '
            f'height="{source_height:.1f}" preserveAspectRatio="xMinYMin meet" '
            f'href="{_embedded_thumbnail_href(source_path, max_width=620, max_height=420)}"/>'
        ),
    ]
    if contact_sheet is not None and contact_sheet.is_file():
        parts.extend(
            [
                '<text x="920" y="122" class="meta">find-object contact sheet</text>',
                (
                    f'<image x="920" y="132" width="{contact_width:.1f}" '
                    f'height="{contact_height:.1f}" '
                    'preserveAspectRatio="xMinYMin meet" '
                    f'href="{_embedded_thumbnail_href(contact_sheet, max_width=980, max_height=420)}"/>'
                ),
            ]
        )
    for layout in card_layouts:
        block: BlockRecord = layout["block"]
        card_y = float(layout["y"])
        card_height = float(layout["height"])
        conflict = bool(layout["conflict"])
        failed = block.raw_job is None or block.raw_job.get("status") != "complete"
        border = "#dc2626" if failed else ("#d97706" if conflict else "#2f855a")
        fill = "#fff7ed" if conflict else "#ffffff"
        parts.append(
            f'<rect x="24" y="{card_y:.1f}" width="1752" '
            f'height="{card_height:.1f}" rx="14" fill="{fill}" '
            f'stroke="{border}" stroke-width="3"/>'
        )
        header = (
            f"{block.object_stem} [{block.object_kind}] / {block.policy} / "
            f"{block.block_id} / crop-index={block.index} / "
            f"segments={len(block.segment_ids)}"
        )
        parts.append(
            f'<text x="42" y="{card_y + 31:.1f}" class="block-title">'
            f"{html.escape(header)}</text>"
        )
        _svg_text(
            parts,
            x=42,
            y=card_y + 57,
            lines=[
                "core: " + ", ".join(block.core_segment_ids or ("-",)),
                "context: " + ", ".join(block.context_segment_ids or ("-",)),
                "all: " + ", ".join(block.segment_ids or ("-",)),
            ],
            css_class="meta",
            line_height=19,
        )
        image_y = card_y + 112
        raw_width, raw_height, raw_scale = layout["raw_size"]
        parts.append(
            (
                f'<image x="{RAW_X}" y="{image_y:.1f}" '
                f'width="{raw_width:.1f}" height="{raw_height:.1f}" '
                'preserveAspectRatio="xMinYMin meet" '
                f'href="{_embedded_png_href(block.raw_path)}"/>'
            )
        )
        if not block.policy.startswith("matrix-orxor"):
            _overlay(
                parts,
                block=block,
                geometry=geometry,
                job=block.raw_job,
                x=RAW_X,
                y=image_y,
                scale=raw_scale,
            )
        text_y = image_y + raw_height + 28
        _svg_text(
            parts,
            x=RAW_X,
            y=text_y,
            lines=layout["raw_lines"],
            css_class="ocr",
        )
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return conflicts, failed_jobs


def _accuracy(item_dir: Path) -> str:
    manifest = item_dir / STAGE_DIRECTORIES["generate-object"] / "manifest.json"
    if not manifest.is_file():
        return "--"
    comparison = _json(manifest).get("comparison")
    if not isinstance(comparison, dict):
        return "--"
    accuracy = comparison.get("accuracy_percent")
    return f"{float(accuracy):.4f}%" if isinstance(accuracy, (int, float)) else "--"


def build_report(run_dir: Path, output: Path, *, blocks_per_page: int) -> None:
    run_dir = run_dir.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.mkdir(parents=True)
    item_output_root = output / "items"
    item_output_root.mkdir()
    report_rows: list[dict[str, Any]] = []
    total_pages = 0
    total_blocks = 0
    total_conflicts = 0
    total_failed_jobs = 0
    for item_id, source, item_dir, geometry_path in _iter_report_items(
        run_dir
    ):
        if _runner_status(item_dir, "ocr-blocks") != "COMPLETE":
            continue
        blocks = _load_blocks(item_dir)
        geometry = _read_segments(geometry_path)
        source_path = source
        contact_sheet = (
            item_dir / STAGE_DIRECTORIES["find-object"] / "_raw" / "contact-sheet.png"
        )
        item_output = item_output_root / item_id
        item_output.mkdir()
        page_count = max(1, math.ceil(len(blocks) / blocks_per_page))
        page_records: list[dict[str, Any]] = []
        item_conflicts = 0
        item_failed_jobs = 0
        for page_index in range(page_count):
            first = page_index * blocks_per_page
            chunk = blocks[first : first + blocks_per_page]
            page_path = item_output / f"page-{page_index + 1:03d}.svg"
            conflicts, failed_jobs = _write_page(
                page_path,
                title=f"{source.name} | OCR blocks {first + 1}-{first + len(chunk)}",
                page_number=page_index + 1,
                page_count=page_count,
                source_path=source_path,
                contact_sheet=contact_sheet if contact_sheet.is_file() else None,
                blocks=chunk,
                geometry=geometry,
            )
            item_conflicts += conflicts
            item_failed_jobs += failed_jobs
            page_records.append(
                {
                    "path": page_path.name,
                    "first_block": first + 1,
                    "last_block": first + len(chunk),
                    "blocks": len(chunk),
                }
            )
        item_index_path = item_output / "index.md"
        item_index = [
            f"# {source.name}",
            "",
            f"- Item: `{item_id}`",
            f"- OCR blocks: {len(blocks)}",
            f"- Raw/gamma conflicts: {item_conflicts}",
            f"- Failed OCR jobs: {item_failed_jobs}",
            f"- get-segment: `{_runner_status(item_dir, 'get-segment')}`",
            f"- generate-object: `{_runner_status(item_dir, 'generate-object')}`",
            f"- Exact accuracy: `{_accuracy(item_dir)}`",
            "",
            "## Исходная картинка",
            "",
            (
                f'<img src="{_relative_href(source_path, item_index_path)}" '
                'width="100%">'
            ),
            "",
        ]
        if contact_sheet.is_file():
            item_index.extend(
                [
                    "## Объекты",
                    "",
                    (
                        f'<img src="{_relative_href(contact_sheet, item_index_path)}" '
                        'width="100%">'
                    ),
                    "",
                ]
            )
        item_index.extend(["## OCR blocks", ""])
        for page in page_records:
            item_index.extend(
                [
                    (
                        f"### Blocks {page['first_block']}-{page['last_block']}"
                    ),
                    "",
                    (
                        f'<a href="{page["path"]}"><img src="{page["path"]}" '
                        'width="100%"></a>'
                    ),
                    "",
                ]
            )
        (item_output / "index.md").write_text(
            "\n".join(item_index) + "\n", encoding="utf-8"
        )
        report_rows.append(
            {
                "item_id": item_id,
                "source": source.name,
                "blocks": len(blocks),
                "pages": page_count,
                "conflicts": item_conflicts,
                "failed_jobs": item_failed_jobs,
                "get_segment": _runner_status(item_dir, "get-segment"),
                "generate_object": _runner_status(item_dir, "generate-object"),
                "accuracy": _accuracy(item_dir),
            }
        )
        total_pages += page_count
        total_blocks += len(blocks)
        total_conflicts += item_conflicts
        total_failed_jobs += item_failed_jobs
    index_lines = [
        "# Визуальный отчёт OCR blocks",
        "",
        (
            "Каждая SVG-страница показывает исходный raster, object contact sheet, "
            "raw/gamma block crops, source segment boundaries и OCR word boxes."
        ),
        "",
        "- Зелёная solid-рамка: core segment.",
        "- Оранжевая solid-рамка: context segment.",
        "- Синяя dashed-рамка: OCR word.",
        "- Красная карточка: separate-block создан, но OCR job отсутствует или упал.",
        "- Суффикс `ocr-fallback`: OCR обошёл показанные separate-block и распознал объект целиком.",
        "",
        f"- Файлов: {len(report_rows)}",
        f"- Блоков: {total_blocks}",
        f"- SVG-страниц: {total_pages}",
        f"- Raw/gamma конфликтов: {total_conflicts}",
        f"- Упавших OCR jobs: {total_failed_jobs}",
        "",
        "| Файл | Blocks | Pages | Missing OCR jobs | get-segment | generate-object | Accuracy | Отчёт |",
        "| --- | ---: | ---: | ---: | --- | --- | ---: | --- |",
    ]
    for row in report_rows:
        link = f"items/{row['item_id']}/index.md"
        index_lines.append(
            f"| `{row['source']}` | {row['blocks']} | {row['pages']} | "
            f"{row['failed_jobs']} | "
            f"`{row['get_segment']}` | `{row['generate_object']}` | "
            f"{row['accuracy']} | [открыть]({link}) |"
        )
    (output / "index.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema": "debug-ocr-block-svg-report-v1",
        "run_dir": str(run_dir),
        "items": report_rows,
        "totals": {
            "items": len(report_rows),
            "blocks": total_blocks,
            "pages": total_pages,
            "raw_gamma_conflicts": total_conflicts,
            "failed_jobs": total_failed_jobs,
            "missing_assets": 0,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--blocks-per-page", type=int, default=BLOCKS_PER_PAGE
    )
    args = parser.parse_args()
    if args.blocks_per_page < 1:
        parser.error("--blocks-per-page must be positive")
    build_report(
        args.run_dir,
        args.output,
        blocks_per_page=args.blocks_per_page,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
