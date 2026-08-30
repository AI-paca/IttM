#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.pipeline_config import resolve_pipeline_profile
from app.pipeline_core.separated import (
    NativeSeparatedSession,
    SeparatedOcrJob,
)
from app.pipeline_core.separated_recognition import recognize_separated_block
from app.services.convert_service import _create_engine, _recognize_text_with_sparse_fallback


STAGE_DIRS = (
    "00-preprocess",
    "01-geometry",
    "02-topology",
    "03-find-object",
    "04-separate-block",
    "05-ocr-blocks",
    "06-get-segment",
    "07-generate-object",
)
STAGE_NAMES = tuple(name.split("-", 1)[1] for name in STAGE_DIRS)


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def job_payload(job: SeparatedOcrJob, text: str = "") -> dict[str, object]:
    return {
        "index": job.index,
        "bbox": job.bbox,
        "object_id": job.object_id,
        "row": job.row,
        "column": job.column,
        "row_span": job.row_span,
        "column_span": job.column_span,
        "recognition_mode": job.recognition_mode,
        "object_kind": job.object_kind,
        "languages": job.languages,
        "transform": job.transform,
        "depth": job.depth,
        "logical_row_count": job.logical_row_count,
        "logical_column_count": job.logical_column_count,
        "grammar_milli": job.grammar_milli,
        "superseded": job.superseded,
        "text": text,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Dump the production native Rust separated route."
    )
    value.add_argument("source", type=Path)
    value.add_argument("--output", required=True, type=Path)
    value.add_argument(
        "--engine",
        choices=("auto", "tesseract", "easyocr"),
        default="tesseract",
    )
    value.add_argument(
        "--to-stage",
        choices=STAGE_NAMES,
        default="generate-object",
    )
    return value


def main() -> int:
    args = parser().parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in STAGE_DIRS:
        (output / name).mkdir(exist_ok=True)

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.save(output / "00-preprocess" / "raster.png")

    target_index = STAGE_NAMES.index(args.to_stage)
    run_blocks = target_index >= STAGE_NAMES.index("separate-block")
    run_ocr = target_index >= STAGE_NAMES.index("ocr-blocks")
    run_get_segment = target_index >= STAGE_NAMES.index("get-segment")
    run_generate = target_index >= STAGE_NAMES.index("generate-object")
    profile = resolve_pipeline_profile(args.engine) if run_ocr else None
    engine = _create_engine(args.engine, profile) if run_ocr else None
    texts: dict[int, str] = {}
    words_by_job: dict[int, list[dict[str, object]]] = {}
    with NativeSeparatedSession(image) as session:
        route_id = session.route_id
        topology = session.topology
        objects = session.objects
        blocks = session.blocks
        if run_blocks:
            for block in blocks:
                block_raster = session.block_raster(block)
                try:
                    block_raster.save(
                        output
                        / "04-separate-block"
                        / f"block-{block.index + 1:03d}.png"
                    )
                finally:
                    block_raster.close()
        jobs = list(session.jobs)
        if run_ocr:
            jobs = []
            index = 0
            while index < session.job_count:
                job = session.job(index)
                jobs.append(job)
                crop = session.raster(job)
                try:
                    assert profile is not None and engine is not None
                    result = recognize_separated_block(
                        crop,
                        job,
                        engine,
                        profile,
                        _recognize_text_with_sparse_fallback,
                    )
                    text = result.text
                    confidence_milli = result.confidence_milli
                    words = result.words
                    crop.save(
                        output
                        / "05-ocr-blocks"
                        / f"block-{job.index + 1:03d}.png"
                    )
                finally:
                    crop.close()
                for word in words:
                    session.add_ocr_word(job.index, word)
                words_by_job[job.index] = [
                    {
                        "text": word.text,
                        "bbox": word.bbox,
                        "confidence_milli": word.confidence_milli,
                    }
                    for word in words
                ]
                session.set_ocr(job.index, text, confidence_milli)
                texts[job.index] = text
                (output / "05-ocr-blocks" / f"block-{job.index + 1:03d}.txt").write_text(
                    text.rstrip() + "\n", encoding="utf-8"
                )
                index += 1
        markdown = session.render() if run_generate else ""
        jobs = [session.job(index) for index in range(session.job_count)]
        segment_records = [
            {
                "job_index": job.index,
                "object_id": job.object_id,
                "object_kind": job.object_kind,
                "block_bbox": job.bbox,
                "text": texts.get(job.index, ""),
                "segments": [
                    {
                        "index": segment.index,
                        "source_bbox": segment.source_bbox,
                        "cell": segment.cell,
                        "crop_bbox": segment.crop_bbox,
                        "placement_source_bbox": segment.placement_source_bbox,
                    }
                    for segment in session.job_segments(job.index)
                ],
            }
            for job in jobs
            if run_get_segment and not job.superseded
        ]
        completed_stages = tuple(
            stage
            for stage in session.completed_stages
            if STAGE_NAMES.index(stage) <= target_index
        )

    structural_jobs = tuple(job for job in jobs if job.depth == 0)
    grouped: dict[int, list[SeparatedOcrJob]] = defaultdict(list)
    for job in jobs:
        if job.superseded:
            continue
        grouped[job.object_id].append(job)
    object_kind_names = {0: "paragraph", 1: "list", 2: "table"}
    for object_value in objects:
        object_image = image.crop(object_value.bbox)
        try:
            object_image.save(
                output
                / "03-find-object"
                / f"object-{object_value.index + 1:03d}-{object_kind_names.get(object_value.object_kind, 'unknown')}.png"
            )
        finally:
            object_image.close()

    if run_blocks:
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        for block in blocks:
            draw.rectangle(
                block.bbox,
                outline="red",
                width=max(2, image.width // 700),
            )
            draw.text(
                (block.bbox[0] + 3, block.bbox[1] + 3),
                str(block.index + 1),
                fill="red",
            )
        overlay.save(output / "04-separate-block" / "overlay.png")
        overlay.close()
    image.close()

    opaque_issue = "Rust core marks this stage complete but does not export its intermediate state."
    write_json(
        output / "01-geometry" / "manifest.json",
        {"stage": "geometry", "status": "opaque", "issue": opaque_issue},
    )
    write_json(
        output / "02-topology" / "manifest.json",
        {
            "stage": "physical-sparse-topology",
            "codes": {
                "nothing": 0,
                "merge_up": 3,
                "merge_left": 5,
                "empty": 7,
                "merge_both": 8,
                "empty_merge_up": 10,
            },
            "rows": [
                {
                    "row": row.index,
                    "y": row.y,
                    "source_matrix_rows": row.source_matrix_rows,
                    "segments": [
                        {
                            "column": column,
                            "x": slot.x,
                            "state": "empty" if slot.empty else "payload",
                            "code": slot.code,
                        }
                        for column, slot in enumerate(row.slots)
                    ],
                    "compressed_codes": [*[slot.code for slot in row.slots], None],
                }
                for row in topology
            ],
        },
    )
    write_json(
        output / "03-find-object" / "manifest.json",
        {
            "stage": "recursive-topology-object-partition",
            "route_id": route_id,
            "objects": [
                {
                    "object_id": value.index,
                    "bbox": value.bbox,
                    "object_kind": value.object_kind,
                    "segment_indexes": value.segment_indexes,
                    "reading_index": value.reading_index,
                    "row_start": value.row_start,
                    "row_stop": value.row_stop,
                    "column_start": value.column_start,
                    "column_stop": value.column_stop,
                }
                for value in objects
            ],
        },
    )
    if run_blocks:
        write_json(
            output / "04-separate-block" / "manifest.json",
            {
                "stage": "separate-block",
                "route_id": route_id,
                "blocks": [
                    {
                        "index": block.index,
                        "object_id": block.object_id,
                        "bbox": block.bbox,
                        "segment_indexes": block.segment_indexes,
                        "dyadic_mask": block.dyadic_mask,
                        "matrix_window": block.matrix_window,
                        "logical_scope_shape": block.logical_scope_shape,
                    }
                    for block in blocks
                ],
                "jobs": [job_payload(job) for job in structural_jobs],
            },
        )
    if run_get_segment:
        write_json(
            output / "06-get-segment" / "segments.json",
            {
                "stage": "get-segment",
                "records": segment_records,
            },
        )
    if run_generate:
        (output / "07-generate-object" / "result.md").write_text(
            markdown.rstrip() + "\n", encoding="utf-8"
        )
    write_json(
        output / "route.json",
        {
            "schema": "ittm-rust-separated-debug-v1",
            "runtime": "rust-native",
            "route_id": route_id,
            "source": str(source),
            "engine": args.engine,
            "to_stage": args.to_stage,
            "completed_stages": completed_stages,
            "jobs": [
                {
                    **job_payload(job, texts.get(job.index, "")),
                    "grammar_milli": job.grammar_milli,
                    "words": words_by_job.get(job.index, []),
                }
                for job in jobs
            ],
            "objects": [
                {
                    "object_id": value.index,
                    "bbox": value.bbox,
                    "object_kind": value.object_kind,
                    "segment_indexes": value.segment_indexes,
                }
                for value in objects
            ],
            "blocks": [
                {
                    "index": block.index,
                    "object_id": block.object_id,
                    "bbox": block.bbox,
                    "segment_indexes": block.segment_indexes,
                    "dyadic_mask": block.dyadic_mask,
                    "matrix_window": block.matrix_window,
                    "logical_scope_shape": block.logical_scope_shape,
                }
                for block in blocks
            ],
            "limitations": [
                "geometry is opaque in the native diagnostic ABI",
                "get-segment exports terminal jobs with source, compact, and logical cell geometry",
            ],
        },
    )

    report = [
        "# Rust separated visual report",
        "",
        "- runtime: `rust-native`",
        f"- engine: `{args.engine}`",
        f"- route_id: `0x{route_id:08x}`",
        "",
        "## Source",
        "",
        '<img src="00-preprocess/raster.png" width="1200" alt="source">',
        "",
    ]
    if run_blocks:
        report.extend(
            [
                "## Separate-block overlay",
                "",
                '<img src="04-separate-block/overlay.png" width="1200" alt="blocks overlay">',
                "",
            ]
        )
    for object_id, object_jobs in sorted(grouped.items()):
        report.extend(
            [
                f"## Object {object_id + 1} (type: {object_kind_names.get(object_jobs[0].object_kind, 'unknown')})",
                "",
                (
                    '<img src="03-find-object/'
                    f"object-{object_id + 1:03d}-{object_kind_names.get(object_jobs[0].object_kind, 'unknown')}.png\" width=\"1200\" "
                    f'alt="object {object_id + 1}">'
                ),
                "",
            ]
        )
        if run_blocks:
            for block in (item for item in blocks if item.object_id == object_id):
                report.extend(
                    [
                        f"### Separate block {block.index + 1}",
                        "",
                        (
                            '<img src="04-separate-block/'
                            f'block-{block.index + 1:03d}.png" width="1200" '
                            f'alt="Separate block {block.index + 1}">'
                        ),
                        "",
                    ]
                )
        for job in object_jobs:
            if not run_ocr:
                continue
            report.extend(
                [
                    f"### OCR job {job.index + 1} (depth {job.depth})",
                    "",
                    (
                        '<img src="05-ocr-blocks/'
                        f'block-{job.index + 1:03d}.png" width="1200" '
                        f'alt="OCR job {job.index + 1}">'
                    ),
                    "",
                    "~~~text",
                    texts.get(job.index, ""),
                    "~~~",
                    "",
                ]
            )
    if run_generate:
        report.extend(["## Generated Markdown", "", markdown.rstrip(), ""])
    (output / "report.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
