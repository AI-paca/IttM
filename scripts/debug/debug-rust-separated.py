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
from app.pipeline_core.separated import NativeSeparatedSession, SeparatedOcrJob
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
        choices=("find-object", "separate-block", "generate-object"),
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

    run_blocks = args.to_stage in {"separate-block", "generate-object"}
    run_ocr = args.to_stage == "generate-object"
    profile = resolve_pipeline_profile(args.engine) if run_ocr else None
    engine = _create_engine(args.engine, profile) if run_ocr else None
    texts: dict[int, str] = {}
    with NativeSeparatedSession(image) as session:
        route_id = session.route_id
        jobs = session.jobs
        for job in jobs:
            if not run_blocks:
                continue
            crop = session.raster(job)
            try:
                crop.save(
                    output
                    / "04-separate-block"
                    / f"block-{job.index + 1:03d}.png"
                )
                if run_ocr:
                    assert profile is not None and engine is not None
                    psm = {
                        0: profile.text_region_psm,
                        1: profile.document_region_psm,
                        2: profile.wide_text_region_psm,
                    }.get(job.recognition_mode, profile.text_region_psm)
                    text = _recognize_text_with_sparse_fallback(
                        engine,
                        crop,
                        profile,
                        mode="text_mode",
                        psm=psm,
                        min_fallback_tokens=(
                            profile.edge_word_fallback_min_tokens
                            if job.recognition_mode == 2
                            else None
                        ),
                    )
                else:
                    text = ""
            finally:
                crop.close()
            if not run_ocr:
                continue
            texts[job.index] = text
            session.set_ocr(job.index, text)
            (output / "05-ocr-blocks" / f"block-{job.index + 1:03d}.txt").write_text(
                text.rstrip() + "\n", encoding="utf-8"
            )
            (output / "06-get-segment" / f"segment-{job.index + 1:03d}.txt").write_text(
                text.rstrip() + "\n", encoding="utf-8"
            )
        markdown = session.render() if run_ocr else ""
        target_index = STAGE_NAMES.index(args.to_stage)
        completed_stages = tuple(
            stage
            for stage in session.completed_stages
            if STAGE_NAMES.index(stage) <= target_index
        )

    grouped: dict[int, list[SeparatedOcrJob]] = defaultdict(list)
    for job in jobs:
        grouped[job.object_id].append(job)
    object_kind_names = {0: "paragraph", 1: "list", 2: "table"}
    for object_id, object_jobs in sorted(grouped.items()):
        left = min(job.bbox[0] for job in object_jobs)
        top = min(job.bbox[1] for job in object_jobs)
        right = max(job.bbox[2] for job in object_jobs)
        bottom = max(job.bbox[3] for job in object_jobs)
        object_image = image.crop((left, top, right, bottom))
        try:
            object_image.save(
                output
                / "03-find-object"
                / f"object-{object_id + 1:03d}-{object_kind_names.get(object_jobs[0].object_kind, 'unknown')}.png"
            )
        finally:
            object_image.close()

    if run_blocks:
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        for job in jobs:
            draw.rectangle(
                job.bbox,
                outline="red",
                width=max(2, image.width // 700),
            )
            draw.text(
                (job.bbox[0] + 3, job.bbox[1] + 3),
                str(job.index + 1),
                fill="red",
            )
        overlay.save(output / "04-separate-block" / "overlay.png")
        overlay.close()
    image.close()

    opaque_issue = (
        "Rust core marks this stage complete but ABI v5 does not export its "
        "intermediate state."
    )
    write_json(
        output / "01-geometry" / "manifest.json",
        {"stage": "geometry", "status": "opaque", "issue": opaque_issue},
    )
    write_json(
        output / "02-topology" / "manifest.json",
        {"stage": "topology", "status": "opaque", "issue": opaque_issue},
    )
    if run_blocks:
        write_json(
            output / "04-separate-block" / "manifest.json",
            {
                "stage": "separate-block",
                "route_id": route_id,
                "jobs": [job_payload(job) for job in jobs],
            },
        )
    if run_ocr:
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
                job_payload(job, texts.get(job.index, "")) for job in jobs
            ],
            "limitations": [
                "geometry and topology are opaque in ABI v5",
                "get-segment currently maps one OCR job to one segment",
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
        for job in object_jobs:
            if not run_blocks:
                continue
            report.extend(
                [
                    f"### OCR block {job.index + 1}",
                    "",
                    (
                        '<img src="04-separate-block/'
                        f'block-{job.index + 1:03d}.png" width="1200" '
                        f'alt="OCR block {job.index + 1}">'
                    ),
                    "",
                    "~~~text",
                    texts.get(job.index, ""),
                    "~~~",
                    "",
                ]
            )
    if run_ocr:
        report.extend(["## Generated Markdown", "", markdown.rstrip(), ""])
    (output / "report.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
