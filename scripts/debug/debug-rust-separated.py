#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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
    SeparatedBlockStageInput,
    SeparatedJobSegment,
    SeparatedOcrStageInput,
    SeparatedOcrJob,
    SeparatedOcrWord,
    SeparatedRecognizedSegment,
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
        "--tessdata",
        type=Path,
        help="Tesseract traineddata directory (defaults to .cache/tessdata when present)",
    )
    value.add_argument(
        "--to-stage",
        choices=STAGE_NAMES,
        default="generate-object",
    )
    value.add_argument(
        "--from-stage",
        type=Path,
        help="Import a saved separate-block directory and execute only later stages",
    )
    return value


def numeric_id(value: str) -> int:
    return int(value.rsplit("-", 1)[-1])


def resolve_stage_input(stage: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = stage / path
    return path.resolve(strict=True)


def page_bbox(value: dict[str, int], offset: tuple[int, int]) -> tuple[int, int, int, int]:
    return (
        int(value["left"]) + offset[0],
        int(value["top"]) + offset[1],
        int(value["right"]) + offset[0],
        int(value["bottom"]) + offset[1],
    )


def job_checkpoint_metadata(
    job: SeparatedOcrJob,
    segments: tuple[SeparatedJobSegment, ...],
) -> tuple[int, ...]:
    source_indexes = tuple(segment.source_index for segment in segments)
    matrix_window = (
        job.row,
        job.row + job.row_span,
        job.column,
        job.column + job.column_span,
    )
    metadata: list[int] = [
        1,
        job.object_id,
        job.object_kind,
        *job.bbox,
        *job.bbox,
        int(any(segment.crop_bbox is not None for segment in segments)),
        int(job.object_kind == 2),
        *matrix_window,
        job.logical_row_count,
        job.logical_column_count,
        len(source_indexes),
        *source_indexes,
        len(segments),
    ]
    for segment in segments:
        metadata.extend((segment.source_index, *segment.source_bbox, *segment.cell))
    placements: dict[
        tuple[tuple[int, int, int, int], tuple[int, int, int, int]], list[int]
    ] = {}
    for segment in segments:
        if segment.placement_source_bbox is None or segment.crop_bbox is None:
            continue
        placements.setdefault(
            (segment.placement_source_bbox, segment.crop_bbox), []
        ).append(segment.index)
    metadata.extend((0, len(placements)))
    for (source_bbox, crop_bbox), indexes in placements.items():
        metadata.extend((*source_bbox, *crop_bbox, len(indexes), *indexes))
    return tuple(metadata)


def source_space_word_bbox(
    bbox: tuple[int, int, int, int],
    job_bbox: tuple[int, int, int, int],
    segments: tuple[SeparatedJobSegment, ...],
) -> tuple[int, int, int, int]:
    placements = []
    seen = set()
    for segment in segments:
        if segment.placement_source_bbox is None or segment.crop_bbox is None:
            continue
        placement = (segment.placement_source_bbox, segment.crop_bbox)
        if placement not in seen:
            seen.add(placement)
            placements.append(placement)
    if not placements:
        return bbox
    left, top, right, bottom = bbox
    selected_index, (source, crop) = max(
        enumerate(placements),
        key=lambda value: (
            max(0, min(right, value[1][1][2]) - max(left, value[1][1][0]))
            * max(0, min(bottom, value[1][1][3]) - max(top, value[1][1][1])),
            -value[0],
        ),
    )
    del selected_index

    def project(value: int, source_start: int, source_stop: int, crop_start: int, crop_stop: int) -> int:
        value = min(max(value, crop_start), crop_stop)
        return source_start + round(
            (value - crop_start)
            * (source_stop - source_start)
            / max(1, crop_stop - crop_start)
        )

    source_bbox = (
        project(left, source[0], source[2], crop[0], crop[2]),
        project(top, source[1], source[3], crop[1], crop[3]),
        project(right, source[0], source[2], crop[0], crop[2]),
        project(bottom, source[1], source[3], crop[1], crop[3]),
    )
    return (
        source_bbox[0] - job_bbox[0],
        source_bbox[1] - job_bbox[1],
        source_bbox[2] - job_bbox[0],
        source_bbox[3] - job_bbox[1],
    )


def load_separate_block_inputs(stage: Path) -> tuple[SeparatedBlockStageInput, ...]:
    stage = stage.resolve(strict=True)
    checkpoint_path = stage / "checkpoint.json"
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        values = []
        for record in checkpoint["records"]:
            with Image.open(stage / record["raster"]) as opened:
                raster = opened.convert("RGB")
                pixels = raster.tobytes()
                raster.close()
            values.append(
                SeparatedBlockStageInput(
                    metadata=tuple(int(value) for value in record["metadata"]),
                    pixels=pixels,
                    width=int(record["width"]),
                    height=int(record["height"]),
                    stride=int(record["stride"]),
                )
            )
        return tuple(values)
    stage_input = json.loads((stage / "input.json").read_text(encoding="utf-8"))
    object_stage = resolve_stage_input(stage, stage_input["input"])
    values: list[SeparatedBlockStageInput] = []
    kind_codes = {"paragraph": 0, "list": 1, "table": 2, "unknown": 3}
    for object_dir in sorted((stage / "objects").iterdir()):
        if not object_dir.is_dir():
            continue
        object_id = numeric_id(object_dir.name.split("-", 2)[1])
        kind_name = object_dir.name.split("-", 2)[2]
        source_dir = object_stage / "objects" / object_dir.name
        object_data = json.loads((source_dir / "object.json").read_text(encoding="utf-8"))
        matrix_data = json.loads((source_dir / "matrix.json").read_text(encoding="utf-8"))
        object_bbox = tuple(int(value) for value in object_data["bbox"])
        offset = (object_bbox[0], object_bbox[1])
        spans: dict[int, list[int]] = {}
        cells_by_segment: dict[int, list[tuple[int, int, tuple[int, int, int, int]]]] = {}
        maximum_column = 0
        for row_data in matrix_data.get("rows", []):
            row = int(row_data["row"])
            row_y = tuple(
                int(value)
                for value in row_data.get("matrix_y", row_data["y"])
            )
            maximum_column = max(
                maximum_column,
                int(row_data.get("null_tail", {}).get("through_column_exclusive", 0)),
            )
            for segment in row_data.get("segments", []):
                column = int(segment["column"])
                column_x = tuple(
                    int(value)
                    for value in segment.get("matrix_x", segment["x"])
                )
                maximum_column = max(maximum_column, column + 1)
                for segment_id in segment.get("source_segment_ids", []):
                    index = numeric_id(segment_id)
                    cells_by_segment.setdefault(index, []).append(
                        (
                            row,
                            column,
                            (
                                column_x[0] + offset[0],
                                row_y[0] + offset[1],
                                column_x[1] + offset[0],
                                row_y[1] + offset[1],
                            ),
                        )
                    )
                    current = spans.setdefault(index, [row, row + 1, column, column + 1])
                    current[0] = min(current[0], row)
                    current[1] = max(current[1], row + 1)
                    current[2] = min(current[2], column)
                    current[3] = max(current[3], column + 1)
        logical_shape = (max(1, len(matrix_data.get("rows", []))), max(1, maximum_column))
        block_files = sorted(object_dir.glob("*/block-*.json"))
        for block_file in block_files:
            block = json.loads(block_file.read_text(encoding="utf-8"))
            raster_path = Path(str(block_file)[: -len(".json")] + ".raw.png")
            with Image.open(raster_path) as opened:
                raster = opened.convert("RGB")
                width, height = raster.size
                pixels = raster.tobytes()
                raster.close()
            placements = block["compaction"]["placements"]
            source_by_segment: dict[int, tuple[int, int, int, int]] = {}
            for placement in placements:
                source = page_bbox(placement["source_bbox"], offset)
                for segment_id in placement["segment_ids"]:
                    source_by_segment[numeric_id(segment_id)] = source
            segment_ids = tuple(numeric_id(value) for value in block["segment_ids"])
            core_ids = tuple(numeric_id(value) for value in block["core_segment_ids"])
            context_ids = tuple(numeric_id(value) for value in block["context_segment_ids"])
            block_bbox = page_bbox(block["bbox"], offset)
            segment_entries: list[
                tuple[int, tuple[int, int, int, int], tuple[int, int, int, int]]
            ] = []
            for fallback_row, segment_id in enumerate(segment_ids):
                if kind_name == "table" and cells_by_segment.get(segment_id):
                    segment_entries.extend(
                        (
                            segment_id,
                            cell_bbox,
                            (row, column, 1, 1),
                        )
                        for row, column, cell_bbox in cells_by_segment[segment_id]
                    )
                    continue
                span = spans.get(segment_id, [fallback_row, fallback_row + 1, 0, 1])
                segment_entries.append(
                    (
                        segment_id,
                        source_by_segment.get(segment_id, block_bbox),
                        (span[0], span[2], span[1] - span[0], span[3] - span[2]),
                    )
                )
            matrix_window = block.get("matrix_window")
            metadata: list[int] = [
                1,
                object_id,
                kind_codes.get(kind_name, 3),
                *object_bbox,
                *block_bbox,
                int(bool(block.get("compacted"))),
                int(matrix_window is not None),
                *(matrix_window or (0, 0, 0, 0)),
                *logical_shape,
                len(core_ids),
                *core_ids,
                len(segment_entries),
            ]
            for segment_id, source, cell in segment_entries:
                metadata.extend(
                    (
                        segment_id,
                        *source,
                        *cell,
                    )
                )
            metadata.extend((len(context_ids), *context_ids, len(placements)))
            for placement in placements:
                placement_source_ids = {
                    numeric_id(value) for value in placement["segment_ids"]
                }
                placement_ids = tuple(
                    index
                    for index, (segment_id, _source, _cell) in enumerate(segment_entries)
                    if segment_id in placement_source_ids
                )
                metadata.extend(
                    (
                        *page_bbox(placement["source_bbox"], offset),
                        *tuple(int(placement["crop_bbox"][key]) for key in ("left", "top", "right", "bottom")),
                        len(placement_ids),
                        *placement_ids,
                    )
                )
            values.append(
                SeparatedBlockStageInput(
                    metadata=tuple(metadata),
                    pixels=pixels,
                    width=width,
                    height=height,
                    stride=width * 3,
                )
            )
    return tuple(values)


def load_ocr_stage_inputs(stage: Path) -> tuple[SeparatedOcrStageInput, ...]:
    stage = stage.resolve(strict=True)
    checkpoint_path = stage / "checkpoint.json"
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        return tuple(
            SeparatedOcrStageInput(
                block_index=int(job["block_index"]),
                languages=str(job["languages"]),
                transform=str(job["transform"]),
                text=str(job.get("text", "")),
                grammar_milli=int(job.get("grammar_milli", 0)),
                words=tuple(
                    SeparatedOcrWord(
                        text=str(word["text"]),
                        bbox=tuple(int(value) for value in word["bbox"]),
                        confidence_milli=int(word["confidence_milli"]),
                    )
                    for word in job.get("words", [])
                ),
                words_in_source_space=bool(
                    job.get("words_in_source_space", False)
                ),
            )
            for job in checkpoint["jobs"]
        )
    block_stage = resolve_stage_input(
        stage,
        json.loads((stage / "input.json").read_text(encoding="utf-8"))["input"],
    )
    selection_path = stage.parent / "06-get-segment" / "segment-topology-selection.json"
    selected_policies: dict[str, str] = {}
    if selection_path.is_file():
        selected_policies = {
            str(object_name): str(policy)
            for object_name, policy in json.loads(
                selection_path.read_text(encoding="utf-8")
            ).get("selected_policies", {}).items()
        }
    block_offset = 0
    values: list[SeparatedOcrStageInput] = []
    for object_dir in sorted((stage / "objects").iterdir()):
        if not object_dir.is_dir():
            continue
        block_object_dir = block_stage / "objects" / object_dir.name
        block_count = len(tuple(block_object_dir.glob("*/block-*.json")))
        jobs_paths = sorted(object_dir.glob("*/jobs.json"))
        selected_policy = selected_policies.get(object_dir.name)
        if selected_policy is not None:
            jobs_paths = [
                jobs_path
                for jobs_path in jobs_paths
                if jobs_path.parent.name == selected_policy
            ]
            if not jobs_paths:
                raise ValueError(
                    f"selected OCR policy is missing for {object_dir.name}: "
                    f"{selected_policy}"
                )
        elif len(jobs_paths) > 1:
            raise ValueError(
                f"ambiguous OCR checkpoint for {object_dir.name}; "
                "run get-segment in the same item so its policy selection is explicit"
            )
        for jobs_path in jobs_paths:
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
            for job in jobs.get("jobs", jobs):
                output = job.get("output") or {}
                words = tuple(
                    SeparatedOcrWord(
                        text=str(word.get("text", "")),
                        bbox=tuple(
                            int(word["bbox"][key])
                            for key in ("left", "top", "right", "bottom")
                        ),
                        # The Rust ABI treats this value as an ordered confidence
                        # unit. Preserve Python's sub-milli ordering at imported
                        # stage boundaries instead of collapsing close candidates
                        # into a lexical tie.
                        confidence_milli=round(float(word.get("confidence", 0.0)) * 1_000_000),
                    )
                    for word in output.get("words", [])
                    if word.get("text")
                )
                values.append(
                    SeparatedOcrStageInput(
                        block_index=block_offset + numeric_id(job["block_id"]),
                        languages=str(job["lane_id"]).replace("-", "+"),
                        transform=(
                            "gamma-dark" if job.get("transform") == "gamma" else "raw"
                        ),
                        text=str(output.get("text", "")),
                        grammar_milli=0,
                        words=words,
                    )
                )
        block_offset += block_count
    return tuple(values)


def load_segment_stage_inputs(stage: Path) -> tuple[SeparatedRecognizedSegment, ...]:
    stage = stage.resolve(strict=True)
    rust_segments = stage / "segments.json"
    if rust_segments.is_file():
        records = json.loads(rust_segments.read_text(encoding="utf-8"))["records"]
        return tuple(
            SeparatedRecognizedSegment(
                index=int(segment["index"]),
                object_id=int(segment["object_id"]),
                object_kind=int(segment["object_kind"]),
                cell=tuple(int(value) for value in segment["cell"]),
                source_segment_indexes=tuple(
                    int(value) for value in segment["source_segment_indexes"]
                ),
                text=str(segment.get("text", "")),
            )
            for segment in records
        )
    handoff = json.loads(
        (stage / "segment-topology-handoff.json").read_text(encoding="utf-8")
    )
    kind_codes = {"paragraph": 0, "list": 1, "table": 2, "unknown": 3}
    values = []
    for index, segment in enumerate(handoff["segments"]):
        topology = segment["topology"]
        values.append(
            SeparatedRecognizedSegment(
                index=index,
                object_id=numeric_id(topology["object_id"]),
                object_kind=kind_codes.get(topology["object_kind"], 3),
                cell=(
                    int(topology["row"]),
                    int(topology["column"]),
                    int(topology["row_span"]),
                    int(topology["column_span"]),
                ),
                source_segment_indexes=tuple(
                    numeric_id(value) for value in segment["source_segment_ids"]
                ),
                text=str(segment.get("text", "")),
            )
        )
    return tuple(values)


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
    imported_ocr: tuple[SeparatedOcrStageInput, ...] | None = None
    imported_segments: tuple[SeparatedRecognizedSegment, ...] | None = None
    block_stage: Path | None = None
    if args.from_stage is not None:
        imported_stage = args.from_stage.resolve(strict=True)
        if imported_stage.name == "04-separate-block" and args.to_stage == "ocr-blocks":
            block_stage = imported_stage
        elif imported_stage.name == "05-ocr-blocks" and args.to_stage == "get-segment":
            block_stage = resolve_stage_input(
                imported_stage,
                json.loads(
                    (imported_stage / "input.json").read_text(encoding="utf-8")
                )["input"],
            )
            imported_ocr = load_ocr_stage_inputs(imported_stage)
        elif imported_stage.name == "06-get-segment" and args.to_stage == "generate-object":
            imported_segments = load_segment_stage_inputs(imported_stage)
        else:
            raise ValueError(
                "Stage import must execute exactly n+1: 04->ocr-blocks, 05->get-segment, or 06->generate-object"
            )
    execute_ocr = run_ocr and imported_ocr is None and imported_segments is None
    tessdata = args.tessdata
    if (
        execute_ocr
        and args.engine in {"auto", "tesseract"}
        and tessdata is None
        and "TESSDATA_PREFIX" not in os.environ
    ):
        candidate = REPO_ROOT / ".cache" / "tessdata"
        if candidate.is_dir():
            tessdata = candidate
    if tessdata is not None:
        tessdata = tessdata.resolve(strict=True)
        if not tessdata.is_dir():
            raise NotADirectoryError(tessdata)
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    profile = resolve_pipeline_profile(args.engine) if execute_ocr else None
    engine = _create_engine(args.engine, profile) if execute_ocr else None
    texts: dict[int, str] = {}
    words_by_job: dict[int, list[dict[str, object]]] = {}
    imported_blocks = (
        load_separate_block_inputs(block_stage)
        if block_stage is not None
        else None
    )
    if imported_segments is not None:
        session_value = NativeSeparatedSession.from_recognized_segments(imported_segments)
    elif imported_blocks is not None:
        session_value = NativeSeparatedSession.from_separate_blocks(imported_blocks)
    else:
        session_value = NativeSeparatedSession(image, start_ocr=False)
    with session_value as session:
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
        if imported_ocr is not None:
            if jobs:
                raise RuntimeError("planned separated stage created jobs before OCR import")
            session.import_ocr(imported_ocr)
            jobs = list(session.jobs)
            for job, imported in zip(jobs, imported_ocr, strict=True):
                texts[job.index] = session.text(job.index)
                words_by_job[job.index] = [
                    {
                        "text": word.text,
                        "bbox": word.bbox,
                        "confidence_milli": word.confidence_milli,
                    }
                    for word in imported.words
                ]
                crop = session.raster(job)
                try:
                    crop.save(
                        output / "05-ocr-blocks" / f"block-{job.index + 1:03d}.png"
                    )
                finally:
                    crop.close()
        elif execute_ocr:
            if jobs:
                raise RuntimeError("planned separated stage created OCR jobs before start_ocr")
            session.start_ocr()
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
                texts[job.index] = session.text(job.index)
                (output / "05-ocr-blocks" / f"block-{job.index + 1:03d}.txt").write_text(
                    text.rstrip() + "\n", encoding="utf-8"
                )
                index += 1
        if run_get_segment:
            if imported_segments is None:
                session.run_get_segment()
            segment_records = [
                {
                    "index": segment.index,
                    "object_id": segment.object_id,
                    "object_kind": segment.object_kind,
                    "cell": segment.cell,
                    "source_segment_indexes": segment.source_segment_indexes,
                    "text": segment.text,
                }
                for segment in session.recognized_segments
            ]
        else:
            segment_records = []
        markdown = session.render() if run_generate else ""
        jobs = [session.job(index) for index in range(session.job_count)]
        job_segments_by_job = {
            job.index: session.job_segments(job.index) for job in jobs
        }
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
        if (
            object_value.bbox[0] >= object_value.bbox[2]
            or object_value.bbox[1] >= object_value.bbox[3]
        ):
            continue
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
        if imported_blocks is not None:
            checkpoint_records = []
            for index, block_input in enumerate(imported_blocks):
                raster_name = f"checkpoint-block-{index + 1:03d}.png"
                raster = Image.frombytes(
                    "RGB",
                    (block_input.width, block_input.height),
                    block_input.pixels,
                )
                try:
                    raster.save(output / "04-separate-block" / raster_name)
                finally:
                    raster.close()
                checkpoint_records.append(
                    {
                        "metadata": block_input.metadata,
                        "raster": raster_name,
                        "width": block_input.width,
                        "height": block_input.height,
                        "stride": block_input.stride,
                    }
                )
            write_json(
                output / "04-separate-block" / "checkpoint.json",
                {
                    "schema": "ittm.rust-separated-block-checkpoint/v1",
                    "records": checkpoint_records,
                },
            )
    if run_ocr:
        selected_jobs = [job for job in jobs if not job.superseded]
        (output / "05-ocr-blocks" / "blocks").mkdir(exist_ok=True)
        checkpoint_blocks = []
        checkpoint_jobs = []
        for block_index, job in enumerate(selected_jobs):
            segments = job_segments_by_job[job.index]
            raster_name = f"checkpoint-block-{block_index + 1:03d}.png"
            with Image.open(
                output / "05-ocr-blocks" / f"block-{job.index + 1:03d}.png"
            ) as opened:
                raster = opened.convert("RGB")
                width, height = raster.size
                raster.save(output / "05-ocr-blocks" / "blocks" / raster_name)
                raster.close()
            checkpoint_blocks.append(
                {
                    "metadata": job_checkpoint_metadata(job, segments),
                    "raster": raster_name,
                    "width": width,
                    "height": height,
                    "stride": width * 3,
                }
            )
            checkpoint_jobs.append(
                {
                    "block_index": block_index,
                    "languages": job.languages,
                    "transform": job.transform,
                    "text": texts.get(job.index, ""),
                    "grammar_milli": job.grammar_milli,
                    "words_in_source_space": False,
                    "words": [
                        {
                            **word,
                            "bbox": source_space_word_bbox(
                                tuple(word["bbox"]), job.bbox, segments
                            ),
                        }
                        for word in words_by_job.get(job.index, [])
                    ],
                }
            )
        write_json(
            output / "05-ocr-blocks" / "input.json",
            {"input": "blocks"},
        )
        write_json(
            output / "05-ocr-blocks" / "blocks" / "checkpoint.json",
            {
                "schema": "ittm.rust-selected-ocr-block-checkpoint/v1",
                "records": checkpoint_blocks,
            },
        )
        write_json(
            output / "05-ocr-blocks" / "checkpoint.json",
            {
                "schema": "ittm.rust-ocr-block-checkpoint/v1",
                "jobs": checkpoint_jobs,
            },
        )
    if run_get_segment:
        write_json(
            output / "06-get-segment" / "input.json",
            {"input": "../05-ocr-blocks"},
        )
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
            "tessdata": str(tessdata) if tessdata is not None else None,
            "to_stage": args.to_stage,
            "completed_stages": completed_stages,
            "jobs": [
                {
                    **job_payload(job, texts.get(job.index, "")),
                    "grammar_milli": job.grammar_milli,
                    "segment_count": len(job_segments_by_job[job.index]),
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
            "segments": segment_records,
            "limitations": [
                "geometry is opaque in the native diagnostic ABI",
                "imported stages expose only the n+1 boundary state",
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
