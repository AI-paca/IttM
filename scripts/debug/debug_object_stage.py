#!/usr/bin/env python3
"""Run one persisted object-recognition debug stage.

This module is intentionally a debug adapter. It calls the existing object
recognition functions, but persists their typed handoff between processes so
that every stage can be stopped, resumed, or fed from another trusted run.
Human-readable PNG/JSON/TXT artifacts are stored beside the machine state.

The state pickle is local debug data, not an interchange format. Only inject
state directories produced by a trusted checkout.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pickle
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPOSITORY_ROOT / "ocr"
DEBUG_ROOT = REPOSITORY_ROOT / "scripts" / "debug"
for import_root in (OCR_ROOT, DEBUG_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import debug_object_recognition as recognition  # noqa: E402
from app.formatting.table_cells import format_markdown_table_cell  # noqa: E402
from app.sparse_pipeline.ocr_adapters import (  # noqa: E402
    TesseractConfig,
    make_tesseract_lane,
)
from app.sparse_pipeline.adaptive_language_ocr import (  # noqa: E402
    AdaptiveOcrContentCache,
    AdaptivePersistentOcrSession as PersistentOcrSession,
)
from app.sparse_pipeline.crop_enhancement import (  # noqa: E402
    CropInput,
    GammaDarkCropEnhancer,
)
from app.sparse_pipeline.ocr_queue import OcrTransform  # noqa: E402
from app.sparse_pipeline.recursive_object_partition import (  # noqa: E402
    horizontal_rule_table_regions,
)

OBJECT_STAGES = (
    "find-object",
    "separate-block",
    "ocr-blocks",
    "get-segment",
    "generate-object",
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_STATE_HANDOFF_SCHEMA = "ittm.debug-object-stage-state"
_STATE_HANDOFF_VERSION = 1


@dataclasses.dataclass(frozen=True)
class _OcrInputArtifact:
    png_bytes: bytes
    source: str
    sha256: str
    expected_sha256: str
    digest_matches: bool
    context_sha256: str
    expected_context_sha256: str
    context_digest_matches: bool


def _safe_name(value: str) -> str:
    return _SAFE_NAME.sub("-", value).strip("-.") or "artifact"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ocr_input_artifact(
    job: Any,
    crop: Any,
    *,
    enhancer: GammaDarkCropEnhancer,
    enhanced_by_block: dict[str, bytes],
    selected_attempt_input: bytes | None = None,
) -> _OcrInputArtifact:
    raw_input = crop.raw.png_bytes
    context_digest = _sha256(raw_input)
    candidates: list[tuple[str, bytes]]
    if job.transform is OcrTransform.RAW:
        candidates = [("raw", raw_input)]
    elif job.transform is OcrTransform.CONTEXTUAL_COMPOSITE:
        candidates = [("contextual-composite", raw_input)]
    elif job.transform is OcrTransform.GAMMA:
        candidates = []
        if crop.gamma is not None:
            candidates.append(("stored-gamma", crop.gamma.png_bytes))
        if not any(_sha256(payload) == job.input_sha256 for _, payload in candidates):
            enhanced = enhanced_by_block.get(job.block_id)
            if enhanced is None:
                enhanced = enhancer.enhance_many(
                    (
                        CropInput(
                            f"{job.block_id}-debug-selected-gamma",
                            raw_input,
                        ),
                    )
                )[0].png_bytes
                enhanced_by_block[job.block_id] = enhanced
            candidates.append(("recomputed-gamma", enhanced))
    elif job.transform is OcrTransform.SOURCE_PLACEMENT_FALLBACK:
        if selected_attempt_input is None:
            raise ValueError(
                "source placement fallback has no captured input artifact"
            )
        candidates = [("source-placement-fallback", selected_attempt_input)]
    else:
        raise ValueError(f"unsupported OCR transform: {job.transform!r}")

    selected_source, selected_input = next(
        (
            (source, payload)
            for source, payload in candidates
            if _sha256(payload) == job.input_sha256
        ),
        candidates[0],
    )
    selected_digest = _sha256(selected_input)
    return _OcrInputArtifact(
        png_bytes=selected_input,
        source=selected_source,
        sha256=selected_digest,
        expected_sha256=job.input_sha256,
        digest_matches=selected_digest == job.input_sha256,
        context_sha256=context_digest,
        expected_context_sha256=job.context_sha256,
        context_digest_matches=context_digest == job.context_sha256,
    )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": _sha256(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_text(
    path: Path,
    value: str,
    *,
    render_markdown: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
    if path.suffix == ".txt" and (
        path.stem in {"result", "segments"}
        or any("ocr-blocks" in part for part in path.parts)
    ):
        markdown = path.with_suffix(".md")
        markdown_temporary = markdown.with_name(
            f".{markdown.name}.partial"
        )
        body = (
            value.rstrip()
            if render_markdown
            else f"```text\n{value.rstrip()}\n```"
        )
        markdown_temporary.write_text(
            f"# {path.stem}\n\n{body}\n",
            encoding="utf-8",
        )
        markdown_temporary.replace(markdown)


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_bytes(value)
    temporary.replace(path)


def _write_state(
    directory: Path,
    value: Any,
    *,
    handoff_kind: str | None = None,
) -> None:
    persisted = (
        {
            "schema": _STATE_HANDOFF_SCHEMA,
            "version": _STATE_HANDOFF_VERSION,
            "kind": handoff_kind,
            "payload": value,
        }
        if handoff_kind is not None
        else value
    )
    payload = pickle.dumps(persisted, protocol=pickle.HIGHEST_PROTOCOL)
    _write_bytes(directory / "state.pkl", payload)
    _write_text(directory / "state.sha256", _sha256(payload) + "\n")


def _read_state(directory: Path) -> Any:
    state_path = directory / "state.pkl"
    digest_path = directory / "state.sha256"
    payload = state_path.read_bytes()
    expected = digest_path.read_text(encoding="utf-8").strip()
    actual = _sha256(payload)
    if actual != expected:
        raise ValueError(
            f"state digest mismatch for {state_path}: expected {expected}, got {actual}"
        )
    state = pickle.loads(payload)
    if not (
        isinstance(state, dict)
        and state.get("schema") == _STATE_HANDOFF_SCHEMA
    ):
        return state
    version = state.get("version")
    if version != _STATE_HANDOFF_VERSION:
        raise ValueError(
            f"unsupported debug state handoff version {version!r} "
            f"for {state_path}"
        )
    kind = state.get("kind")
    if type(kind) is not str or not kind:
        raise ValueError(f"debug state handoff kind is invalid for {state_path}")
    if "payload" not in state:
        raise ValueError(f"debug state handoff payload is missing for {state_path}")
    return state["payload"]


def _prepare_output(
    output: Path,
    *,
    replace: bool,
    allow_raw_child: bool = False,
) -> None:
    if output.exists():
        if replace:
            shutil.rmtree(output)
        elif allow_raw_child and {
            child.name for child in output.iterdir()
        } <= {"_raw"}:
            return
        else:
            raise FileExistsError(f"stage output already exists: {output}")
    output.mkdir(parents=True, exist_ok=True)


def _stage_manifest(
    output: Path,
    *,
    stage: str,
    input_path: Path,
    status: str,
    artifacts: int,
    issues: Iterable[str] = (),
) -> None:
    _write_json(
        output / "manifest.json",
        {
            "schema": "debug-object-stage-v1",
            "stage": stage,
            "status": status,
            "input": str(input_path.resolve()),
            "artifacts": artifacts,
            "issues": tuple(issues),
        },
    )


def _object_root(path: Path) -> Path:
    candidate = path / "objects"
    return candidate if candidate.is_dir() else path


def _state_directories(path: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            state.parent
            for state in path.rglob("state.pkl")
            if state.parent.name != "_raw"
        )
    )


def _stored_stem(stored: recognition.StoredObject) -> str:
    return f"{stored.source_object_id}-{_safe_name(stored.source_kind)}"


def _matrix_table_evidence(
    payload: Mapping[str, Any],
    source_boxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return {
            "detected": False,
            "matrix_height": 0,
            "repeated_row_signature": (),
            "repetitions": 0,
            "independent_repetitions": 0,
        }
    matrix_box = payload.get("matrix_bbox_in_crop")
    matrix_height = (
        matrix_box[3] - matrix_box[1]
        if isinstance(matrix_box, list)
        and len(matrix_box) == 4
        and all(type(item) is int for item in matrix_box)
        else 0
    )
    signatures: Counter[tuple[tuple[int, int], ...]] = Counter()
    signature_sources: defaultdict[
        tuple[tuple[int, int], ...],
        list[frozenset[str]],
    ] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_segments = row.get("segments")
        if not isinstance(raw_segments, list):
            continue
        intervals: list[tuple[int, int]] = []
        row_source_ids: set[str] = set()
        for segment in raw_segments:
            if not isinstance(segment, Mapping):
                continue
            source_ids = segment.get("source_segment_ids")
            if not isinstance(source_ids, list) or not source_ids:
                continue
            row_source_ids.update(
                source_id
                for source_id in source_ids
                if isinstance(source_id, str)
            )
            boxes = tuple(
                source_boxes[source_id]
                for source_id in source_ids
                if source_id in source_boxes
            )
            if boxes:
                intervals.append(
                    (
                        min(box[0] for box in boxes),
                        max(box[2] for box in boxes),
                    )
                )
        signature = tuple(sorted(set(intervals)))
        if len(signature) >= 3:
            signatures[signature] += 1
            signature_sources[signature].append(
                frozenset(row_source_ids)
            )
    signature, repetitions = (
        signatures.most_common(1)[0] if signatures else ((), 0)
    )
    independent_repetitions = len(
        set(signature_sources.get(signature, ()))
    )
    return {
        "detected": (
            repetitions >= 5
            and independent_repetitions >= 5
            and matrix_height >= 100
        ),
        "matrix_height": matrix_height,
        "repeated_row_signature": signature,
        "repetitions": repetitions,
        "independent_repetitions": independent_repetitions,
    }


def _horizontal_rule_table_regions(
    geometry_dir: Path,
) -> tuple[tuple[int, int, int, int], ...]:
    rules_path = geometry_dir / "rules.jsonl"
    manifest_path = geometry_dir / "manifest.json"
    if not rules_path.is_file() or not manifest_path.is_file():
        return ()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    aligned_size = manifest.get("aligned_size")
    if (
        not isinstance(aligned_size, list)
        or len(aligned_size) != 2
        or type(aligned_size[0]) is not int
    ):
        return ()
    if type(aligned_size[1]) is not int:
        return ()
    rules: list[tuple[int, int, int, int]] = []
    for raw_line in rules_path.read_text(encoding="utf-8").splitlines():
        value = json.loads(raw_line)
        bbox = value.get("bbox")
        if (
            value.get("axis") == "horizontal"
            and isinstance(bbox, Mapping)
            and all(type(bbox.get(key)) is int for key in ("left", "top", "right", "bottom"))
        ):
            box = (
                bbox["left"],
                bbox["top"],
                bbox["right"],
                bbox["bottom"],
            )
            rules.append(box)
    return tuple(
        region.bbox
        for region in horizontal_rule_table_regions(
            rules=tuple(rules),
            page_bbox=(0, 0, aligned_size[0], aligned_size[1]),
        )
    )


def _run_find_object(args: argparse.Namespace) -> int:
    if args.geometry_dir is None or args.objects_dir is None:
        raise ValueError("find-object requires --geometry-dir and --objects-dir")
    output = args.output.resolve()
    _prepare_output(output, replace=args.replace, allow_raw_child=True)
    geometry_dir = args.geometry_dir.resolve(strict=True)
    source_root = _object_root(args.objects_dir.resolve(strict=True))
    segments_path = geometry_dir / "segments.jsonl"
    object_directories = tuple(
        sorted(path for path in source_root.glob("object-*") if path.is_dir())
    )
    if not object_directories:
        raise ValueError(f"no object directories found in {source_root}")

    stage_issues: list[str] = []
    records: list[dict[str, Any]] = []
    for source_dir in object_directories:
        stored = recognition.load_stored_object(
            source_dir,
            segments_path=segments_path,
        )
        stem = _stored_stem(stored)
        target = output / "objects" / stem
        target.mkdir(parents=True, exist_ok=False)
        stored = dataclasses.replace(stored, source_dir=target)
        _write_bytes(target / f"{stem}.png", stored.image_png)
        _write_json(target / "object.json", stored.record)
        _write_json(target / "matrix.json", stored.matrix_payload)
        for optional_name in ("matrix.txt", "ownership.png"):
            source = source_dir / optional_name
            if source.is_file():
                shutil.copyfile(source, target / optional_name)
        _write_state(target, stored)
        table_evidence = _matrix_table_evidence(
            stored.matrix_payload,
            {
                segment.segment_id: (
                    segment.bbox.left,
                    segment.bbox.top,
                    segment.bbox.right,
                    segment.bbox.bottom,
                )
                for segment in stored.segments
            },
        )
        issues: list[str] = []
        if table_evidence["detected"] and stored.source_kind != "table":
            issues.append(
                "stable multi-column sparse matrix was not classified as table"
            )
        stage_issues.extend(f"{stem}: {issue}" for issue in issues)
        record = {
            "object_id": stored.source_object_id,
            "kind": stored.source_kind,
            "bbox": tuple(stored.record.get("bbox", ())),
            "stem": stem,
            "segment_ids": tuple(
                segment.segment_id for segment in stored.segments
            ),
            "source": str(source_dir),
            "table_evidence": table_evidence,
            "issues": issues,
        }
        _write_json(target / "manifest.json", record)
        records.append(record)

    for left, top, right, bottom in _horizontal_rule_table_regions(geometry_dir):
        if any(
            record["kind"] == "table"
            and isinstance(record.get("bbox"), tuple)
            and len(record["bbox"]) == 4
            and record["bbox"][0] < right
            and left < record["bbox"][2]
            and record["bbox"][1] < bottom
            and top < record["bbox"][3]
            for record in records
        ):
            continue
        stage_issues.append(
            "stable horizontal-rule table region has no table object: "
            f"{left}:{top}:{right}:{bottom}"
        )

    _write_json(output / "objects.json", records)
    raw_manifest_path = source_root.parent / "manifest.json"
    if raw_manifest_path.is_file():
        raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
        if raw_manifest.get("exact_accounting") is not True:
            stage_issues.append("object extraction exact_accounting is not true")
        _write_json(output / "source-manifest.json", raw_manifest)

    status = "failed" if stage_issues else "complete"
    _stage_manifest(
        output,
        stage="find-object",
        input_path=args.objects_dir,
        status=status,
        artifacts=len(records),
        issues=stage_issues,
    )
    print(output)
    return 3 if stage_issues else 0


def _block_payload(
    block: Any,
    crop: Any,
    *,
    index: int,
    compaction: Any | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "block_id": block.block_id,
        "bbox": block.bbox,
        "crop_bbox": crop.bbox,
        "compacted": crop.bbox != block.bbox,
        "segment_ids": block.segment_ids,
        "core_segment_ids": block.core_segment_ids,
        "context_segment_ids": block.context_segment_ids,
        "object_ids": block.object_ids,
        "scope_id": block.scope_id,
        "matrix_window": block.matrix_window,
        "matrix_window_kind": block.matrix_window_kind,
        "matrix_segment_shape": block.matrix_segment_shape,
        "local_islands": tuple(
            {
                "local_region_id": island.local_region_id,
                "island_id": island.island_id,
                "segment_ids": island.segment_ids,
                "region_boundary": island.region_boundary,
                "matrix_segment_shape": island.matrix_segment_shape,
            }
            for island in block.local_islands
        ),
        "local_placements": tuple(
            {
                "segment_id": placement.segment_id,
                "local_region_id": placement.local_region_id,
                "island_id": placement.island_id,
                "polar_order": placement.polar_order,
                "region_order": placement.region_order,
                "table_row": placement.table_row,
                "table_column": placement.table_column,
                "matrix_row": placement.matrix_row,
                "matrix_column": placement.matrix_column,
            }
            for placement in block.local_placements
        ),
        "masked_segment_ids": crop.masked_segment_ids,
        "compaction": (
            {
                "omitted_empty_units": compaction.omitted_empty_units,
                "occupied_pixels_before": compaction.occupied_pixels_before,
                "occupied_pixels_after": compaction.occupied_pixels_after,
                "packed_canvas_pixels": compaction.packed_canvas_pixels,
                "raster_kind": compaction.raster_kind.value,
                "raw_sha256": compaction.raw_sha256,
                "placements": tuple(
                    {
                        "unit_id": placement.unit_id,
                        "segment_ids": placement.segment_ids,
                        "source_bbox": placement.source_bbox,
                        "crop_bbox": placement.crop_bbox,
                    }
                    for placement in compaction.placements
                ),
            }
            if compaction is not None
            else None
        ),
    }


def _run_separate_block(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    input_path = args.input.resolve(strict=True)
    _prepare_output(output, replace=args.replace)
    state_directories = _state_directories(input_path)
    if not state_directories:
        raise ValueError(f"no find-object state found in {input_path}")

    stage_issues: list[str] = []
    policy_count = 0
    for state_directory in state_directories:
        stored = _read_state(state_directory)
        if not isinstance(stored, recognition.StoredObject):
            continue
        object_stem = _stored_stem(stored)
        for policy in recognition._object_policies(
            stored,
            paragraph_list_ab=args.paragraph_list_ab,
        ):
            separated = recognition.separate_blocks(stored, policy=policy)
            target = output / "objects" / object_stem / policy
            target.mkdir(parents=True, exist_ok=False)
            _write_state(target, (stored, separated))
            blocks_by_id = {
                block.block_id: block for block in separated.plan.blocks
            }
            compaction_by_id = {
                compaction.block_id: compaction
                for compaction in separated.compactions
            }
            singleton_ids: list[str] = []
            for index, crop in enumerate(separated.crops, start=1):
                block = blocks_by_id[crop.block_id]
                width = crop.bbox.right - crop.bbox.left
                height = crop.bbox.bottom - crop.bbox.top
                stem = (
                    f"block-{index:06d}-segments-{len(block.segment_ids):03d}"
                    f"-{width}x{height}"
                )
                _write_bytes(target / f"{stem}.raw.png", crop.raw.png_bytes)
                if crop.gamma is not None:
                    _write_bytes(
                        target / f"{stem}.gamma.png",
                        crop.gamma.png_bytes,
                    )
                if crop.isolation_mask_png is not None:
                    _write_bytes(
                        target / f"{stem}.mask.png",
                        crop.isolation_mask_png,
                    )
                _write_json(
                    target / f"{stem}.json",
                    _block_payload(
                        block,
                        crop,
                        index=index,
                        compaction=compaction_by_id.get(block.block_id),
                    ),
                )
                if (
                    len(block.segment_ids) < 2
                    and stored.source_kind not in {"paragraph", "flow"}
                ):
                    singleton_ids.append(block.block_id)

            missing_overlap = (
                len(separated.plan.blocks) > 1
                and not separated.plan.adjacent_algebra
            )
            issues = [
                f"singleton block forbidden for {stored.source_kind}: {block_id}"
                for block_id in singleton_ids
            ]
            if missing_overlap:
                issues.append(
                    "multiple blocks have no adjacent AND/XOR overlap algebra"
                )
            status = "failed" if issues else "complete"
            stage_issues.extend(
                f"{object_stem}/{policy}: {issue}" for issue in issues
            )
            _write_json(
                target / "manifest.json",
                {
                    "schema": "debug-separated-blocks-v1",
                    "stage": "separate-block",
                    "status": status,
                    "object_id": stored.source_object_id,
                    "object_kind": stored.source_kind,
                    "policy": policy,
                    "block_count": len(separated.plan.blocks),
                    "block_segment_counts": tuple(
                        len(block.segment_ids)
                        for block in separated.plan.blocks
                    ),
                    "timing_seconds": {
                        "planning": separated.planning_seconds,
                        "crop": separated.crop_seconds,
                        "total": (
                            separated.planning_seconds
                            + separated.crop_seconds
                        ),
                    },
                    "compaction": {
                        "blocks": len(separated.compactions),
                        "omitted_empty_units": len(
                            {
                                unit_id
                                for item in separated.compactions
                                for unit_id in item.omitted_empty_units
                            }
                        ),
                        "omitted_empty_memberships": sum(
                            len(item.omitted_empty_units)
                            for item in separated.compactions
                        ),
                        "occupied_pixels_before": sum(
                            item.occupied_pixels_before
                            for item in separated.compactions
                        ),
                        "occupied_pixels_after": sum(
                            item.occupied_pixels_after
                            for item in separated.compactions
                        ),
                        "packed_canvas_pixels": sum(
                            item.packed_canvas_pixels
                            for item in separated.compactions
                        ),
                    },
                    "adjacent_algebra": separated.plan.adjacent_algebra,
                    "membership_units": separated.plan.membership_units,
                    "diagnostics": separated.plan.diagnostics,
                    "issues": issues,
                },
            )
            policy_count += 1

    status = "failed" if stage_issues else "complete"
    _stage_manifest(
        output,
        stage="separate-block",
        input_path=input_path,
        status=status,
        artifacts=policy_count,
        issues=stage_issues,
    )
    print(output)
    return 3 if stage_issues else 0


def _run_ocr_blocks(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    input_path = args.input.resolve(strict=True)
    _prepare_output(output, replace=args.replace)
    state_directories = _state_directories(input_path)
    if not state_directories:
        raise ValueError(f"no separate-block state found in {input_path}")

    languages = tuple(
        item.strip() for item in args.languages.split(",") if item.strip()
    )
    if not languages:
        raise ValueError("--languages must contain at least one language")
    shared_config = {
        "executable": args.tesseract_executable,
        "tessdata_directory": (
            args.tessdata.resolve() if args.tessdata is not None else None
        ),
        "languages": languages,
        "psm": args.tesseract_psm,
        "recognition_miss_retry_max_height": 128,
        "recognition_miss_retry_padding": 16,
    }
    paragraph_lane = make_tesseract_lane(
        "tesseract-debug-paragraph",
        config=TesseractConfig(
            **shared_config,
            upscale_min_height=512,
            upscale_max_factor=8,
        ),
        max_workers=args.tesseract_workers,
    )
    table_config = dict(shared_config)
    table_config["psm"] = 6
    table_lane = make_tesseract_lane(
        "tesseract-debug-table",
        config=TesseractConfig(
            **table_config,
            upscale_min_height=96,
            upscale_max_factor=4,
        ),
        max_workers=args.tesseract_workers,
    )

    stage_issues: list[str] = []
    policy_count = 0
    content_cache = AdaptiveOcrContentCache()
    paragraph_session = PersistentOcrSession(
        (paragraph_lane,),
        log_path=output / "splay.csv",
        content_cache=content_cache,
    )
    table_session = PersistentOcrSession(
        (table_lane,),
        state=paragraph_session.state,
        log_path=output / "splay.csv",
        content_cache=content_cache,
    )
    enhancer = GammaDarkCropEnhancer()
    with (
        paragraph_session,
        table_session,
    ):
        completed_results: list[
            tuple[
                recognition.StoredObject,
                recognition.BlockOcrResult,
                Path,
                PersistentOcrSession,
            ]
        ] = []
        pending_states: list[
            tuple[
                recognition.StoredObject,
                recognition.SeparatedBlocks,
                str | None,
            ]
        ] = []
        for state_directory in state_directories:
            state = _read_state(state_directory)
            if not (
                isinstance(state, tuple)
                and len(state) == 2
                and isinstance(state[0], recognition.StoredObject)
                and isinstance(state[1], recognition.SeparatedBlocks)
            ):
                continue
            pending_states.append((state[0], state[1], None))
        processed_policies: set[tuple[str, str]] = set()
        while pending_states:
            stored, separated, lazy_fallback_reason = pending_states.pop(0)
            policy_key = (stored.source_object_id, separated.policy)
            if policy_key in processed_policies:
                continue
            processed_policies.add(policy_key)
            separated = recognition.canonicalize_locality_separated(
                stored,
                separated,
            )
            object_stem = _stored_stem(stored)
            session = (
                table_session
                if stored.source_kind == "table"
                else paragraph_session
            )
            fallback_reason = _table_whole_object_ocr_reason(
                stored,
                separated,
            )
            ocr_input = separated
            if fallback_reason is not None:
                ocr_input = recognition.separate_blocks(
                    stored,
                    policy="whole-object",
                )
                inherited_diagnostics = tuple(
                    item
                    for item in separated.plan.diagnostics
                    if item.startswith("matrix-logical-")
                )
                ocr_input = dataclasses.replace(
                    ocr_input,
                    plan=dataclasses.replace(
                        ocr_input.plan,
                        diagnostics=(
                            *ocr_input.plan.diagnostics,
                            *inherited_diagnostics,
                            f"ocr-table-fallback={fallback_reason}",
                        ),
                    ),
                )
            block_ocr = recognition.ocr_blocks(
                ocr_input,
                session=session,
            )
            target = (
                output
                / "objects"
                / object_stem
                / block_ocr.separated.policy
            )
            target.mkdir(parents=True, exist_ok=False)
            _write_state(
                target,
                (stored, block_ocr),
                handoff_kind="ocr-blocks/get-segment",
            )
            _write_json(target / "jobs.json", block_ocr.queue)
            completed_blocks: set[str] = set()
            failed_jobs: list[str] = []
            input_issues: list[str] = []
            input_artifacts: list[dict[str, Any]] = []
            crop_by_block = {
                crop.block_id: crop
                for crop in block_ocr.separated.crops
            }
            enhanced_by_block: dict[str, bytes] = {}
            for job in block_ocr.queue.jobs:
                job_stem = _safe_name(job.job_id)
                _write_json(target / "jobs" / f"{job_stem}.json", job)
                crop = crop_by_block[job.block_id]
                input_artifact = _ocr_input_artifact(
                    job,
                    crop,
                    enhancer=enhancer,
                    enhanced_by_block=enhanced_by_block,
                    selected_attempt_input=session.state.attempt_input_payload(
                        job.input_sha256
                    ),
                )
                input_file = f"{job_stem}.input.png"
                _write_bytes(
                    target / "jobs" / input_file,
                    input_artifact.png_bytes,
                )
                input_artifacts.append(
                    {
                        "job_id": job.job_id,
                        "block_id": job.block_id,
                        "profile": job.lane_id,
                        "transform": job.transform.value,
                        "file": f"jobs/{input_file}",
                        "artifact_source": input_artifact.source,
                        "sha256": input_artifact.sha256,
                        "expected_sha256": input_artifact.expected_sha256,
                        "digest_matches": input_artifact.digest_matches,
                        "context_sha256": input_artifact.context_sha256,
                        "expected_context_sha256": (
                            input_artifact.expected_context_sha256
                        ),
                        "context_digest_matches": (
                            input_artifact.context_digest_matches
                        ),
                    }
                )
                if not input_artifact.digest_matches:
                    failed_jobs.append(job.job_id)
                    input_issues.append(
                        f"OCR input digest mismatch for {job.job_id}"
                    )
                if not input_artifact.context_digest_matches:
                    failed_jobs.append(job.job_id)
                    input_issues.append(
                        f"OCR context digest mismatch for {job.job_id}"
                    )
                if job.status.value == "complete" and job.output is not None:
                    completed_blocks.add(job.block_id)
                    _write_text(
                        target / "jobs" / f"{job_stem}.txt",
                        job.output.text,
                    )
                    mean_confidence = (
                        sum(
                            word.confidence
                            for word in job.output.words
                        )
                        / len(job.output.words)
                        if job.output.words
                        else None
                    )
                    confidence_text = (
                        f"{mean_confidence:.2f}"
                        if mean_confidence is not None
                        else "N/A"
                    )
                    _write_text(
                        target / "jobs" / f"{job_stem}.md",
                        (
                            f"# {job.job_id}\n\n"
                            f"![Фактический вход OCR]({input_file})\n\n"
                            f"- Block: `{job.block_id}`\n"
                            f"- Profile: `{job.lane_id}`\n"
                            f"- Transform: `{job.transform.value}`\n"
                            f"- Mean word confidence: `{confidence_text}`\n"
                            f"- Input artifact: `{input_artifact.source}`\n"
                            f"- Input SHA-256: `{input_artifact.sha256}`\n"
                            f"- Context SHA-256: `{input_artifact.context_sha256}`\n\n"
                            f"```text\n{job.output.text.rstrip()}\n```\n"
                        ),
                    )
                else:
                    failed_jobs.append(job.job_id)
                    _write_text(
                        target / "jobs" / f"{job_stem}.md",
                        (
                            f"# {job.job_id}\n\n"
                            f"![Фактический вход OCR]({input_file})\n\n"
                            f"- Block: `{job.block_id}`\n"
                            f"- Profile: `{job.lane_id}`\n"
                            f"- Transform: `{job.transform.value}`\n"
                            f"- Status: `{job.status.value}`\n"
                            f"- Error: `{job.error_message or 'none'}`\n"
                            f"- Input artifact: `{input_artifact.source}`\n"
                            f"- Input SHA-256: `{input_artifact.sha256}`\n"
                            f"- Context SHA-256: `{input_artifact.context_sha256}`\n"
                        ),
                    )
            _write_json(target / "ocr-inputs.json", input_artifacts)
            required_blocks = {
                block.block_id for block in block_ocr.separated.plan.blocks
            }
            missing_blocks = sorted(required_blocks - completed_blocks)
            warnings = [
                f"OCR job failed: {job_id}" for job_id in failed_jobs
            ]
            issues = [
                f"block has no successful OCR candidate: {block_id}"
                for block_id in missing_blocks
            ]
            issues.extend(input_issues)
            status = "failed" if issues else "complete"
            stage_issues.extend(
                f"{object_stem}/{block_ocr.separated.policy}: {issue}"
                for issue in issues
            )
            _write_json(
                target / "manifest.json",
                {
                    "schema": "debug-ocr-blocks-v1",
                    "stage": "ocr-blocks",
                    "status": status,
                    "object_id": stored.source_object_id,
                    "object_kind": stored.source_kind,
                    "policy": block_ocr.separated.policy,
                    "input_policy": separated.policy,
                    "table_whole_object_fallback": fallback_reason,
                    "lazy_paragraph_list_fallback": lazy_fallback_reason,
                    "jobs": len(block_ocr.queue.jobs),
                    "complete_jobs": block_ocr.queue.complete,
                    "failed_jobs": block_ocr.queue.failed,
                    "ocr_wall_seconds": block_ocr.ocr_seconds,
                    "ocr_work_seconds": block_ocr.cache_metrics.get(
                        "ocr_work_seconds",
                        block_ocr.ocr_seconds,
                    ),
                    "cache_requests": block_ocr.cache_metrics.get(
                        "requests",
                        0,
                    ),
                    "cache_hits": block_ocr.cache_metrics.get("hits", 0),
                    "cache_misses": block_ocr.cache_metrics.get("misses", 0),
                    "exact_duplicate_calls": block_ocr.cache_metrics.get(
                        "exact_duplicate_calls_avoided",
                        0,
                    ),
                    "missing_blocks": missing_blocks,
                    "warnings": warnings,
                    "issues": issues,
                },
            )
            completed_results.append((stored, block_ocr, target, session))
            policy_count += 1
            if (
                stored.source_kind in {"paragraph", "list"}
                and separated.policy
                == recognition.canonical_paragraph_list_policy(stored)
            ):
                probe = recognition.get_segments(stored, block_ocr)
                reason = (
                    "explicit-ab-debug-flag"
                    if args.paragraph_list_ab
                    else recognition.lazy_paragraph_list_fallback_reason(probe)
                )
                alternate = recognition.alternate_paragraph_list_policy(
                    stored
                )
                alternate_key = (stored.source_object_id, alternate)
                if reason is not None and alternate_key not in processed_policies:
                    alternate_input = recognition.separate_blocks(
                        stored,
                        policy=alternate,
                    )
                    alternate_input = dataclasses.replace(
                        alternate_input,
                        plan=dataclasses.replace(
                            alternate_input.plan,
                            diagnostics=(
                                *alternate_input.plan.diagnostics,
                                f"lazy-policy-fallback={reason}",
                            ),
                        ),
                    )
                    pending_states.append(
                        (stored, alternate_input, reason)
                    )

        topology_evidence = {}
        for stored, block_ocr, _target, session in completed_results:
            if stored.source_kind != "table" and not any(
                (block.matrix_window_kind or "").startswith("polar-")
                for block in block_ocr.separated.plan.blocks
            ):
                continue
            for item in session.collect_topology_script_evidence(
                plan=block_ocr.separated.plan,
                compactions=block_ocr.separated.compactions,
                queue=block_ocr.queue,
            ):
                key = (
                    item.script_kind,
                    item.matrix_sha256,
                    item.island_id,
                    item.matrix_columns,
                    item.source_left_ppm,
                    item.source_right_ppm,
                )
                previous = topology_evidence.get(key)
                if previous is None or item.confidence > previous.confidence:
                    topology_evidence[key] = item
        ordered_evidence = tuple(
            sorted(
                topology_evidence.values(),
                key=lambda item: (
                    item.script_kind,
                    item.matrix_sha256,
                    item.island_id,
                    item.matrix_columns,
                    item.source_left_ppm,
                    item.source_right_ppm,
                ),
            )
        )
        for stored, block_ocr, target, session in sorted(
            completed_results,
            key=lambda item: (_stored_stem(item[0]), item[1].separated.policy),
        ):
            fusion = session.apply_topology_source_fusion(
                plan=block_ocr.separated.plan,
                crops=block_ocr.separated.crops,
                compactions=block_ocr.separated.compactions,
                queue=block_ocr.queue,
                evidence=ordered_evidence,
            )
            if not fusion.changed_jobs:
                continue
            merged_metrics = dict(block_ocr.cache_metrics)
            for key, value in fusion.cache_metrics.items():
                merged_metrics[key] = merged_metrics.get(key, 0) + value
            patched = dataclasses.replace(
                block_ocr,
                queue=fusion.queue,
                ocr_seconds=block_ocr.ocr_seconds + fusion.elapsed_seconds,
                cache_metrics=merged_metrics,
            )
            _write_state(
                target,
                (stored, patched),
                handoff_kind="ocr-blocks/get-segment",
            )
            _write_json(target / "jobs.json", patched.queue)
            crop_by_block = {
                crop.block_id: crop for crop in patched.separated.crops
            }
            input_artifacts = json.loads(
                (target / "ocr-inputs.json").read_text(encoding="utf-8")
            )
            input_by_job = {
                str(item["job_id"]): item for item in input_artifacts
            }
            for job in patched.queue.jobs:
                if job.job_id not in fusion.changed_jobs:
                    continue
                job_stem = _safe_name(job.job_id)
                crop = crop_by_block[job.block_id]
                input_artifact = _ocr_input_artifact(
                    job,
                    crop,
                    enhancer=enhancer,
                    enhanced_by_block={},
                    selected_attempt_input=session.state.attempt_input_payload(
                        job.input_sha256
                    ),
                )
                input_file = f"{job_stem}.input.png"
                _write_json(target / "jobs" / f"{job_stem}.json", job)
                _write_bytes(target / "jobs" / input_file, input_artifact.png_bytes)
                _write_text(
                    target / "jobs" / f"{job_stem}.txt",
                    job.output.text if job.output is not None else "",
                )
                confidence = (
                    sum(word.confidence for word in job.output.words)
                    / len(job.output.words)
                    if job.output is not None and job.output.words
                    else None
                )
                _write_text(
                    target / "jobs" / f"{job_stem}.md",
                    (
                        f"# {job.job_id}\n\n"
                        f"![Фактический вход OCR]({input_file})\n\n"
                        f"- Block: `{job.block_id}`\n"
                        f"- Profile: `{job.lane_id}`\n"
                        f"- Transform: `{job.transform.value}`\n"
                        "- Topology source fusion: `observed TSV spans`\n"
                        f"- Mean word confidence: `"
                        f"{confidence:.2f}`\n" if confidence is not None else
                        "- Mean word confidence: `N/A`\n"
                    )
                    + f"- Input artifact: `{input_artifact.source}`\n"
                    f"- Input SHA-256: `{input_artifact.sha256}`\n"
                    f"- Context SHA-256: `{input_artifact.context_sha256}`\n\n"
                    f"```text\n{job.output.text.rstrip() if job.output else ''}\n```\n",
                )
                input_by_job[job.job_id] = {
                    "job_id": job.job_id,
                    "block_id": job.block_id,
                    "profile": job.lane_id,
                    "transform": job.transform.value,
                    "file": f"jobs/{input_file}",
                    "artifact_source": input_artifact.source,
                    "sha256": input_artifact.sha256,
                    "expected_sha256": input_artifact.expected_sha256,
                    "digest_matches": input_artifact.digest_matches,
                    "context_sha256": input_artifact.context_sha256,
                    "expected_context_sha256": (
                        input_artifact.expected_context_sha256
                    ),
                    "context_digest_matches": (
                        input_artifact.context_digest_matches
                    ),
                }
            _write_json(
                target / "ocr-inputs.json",
                [
                    input_by_job[job.job_id]
                    for job in patched.queue.jobs
                    if job.job_id in input_by_job
                ],
            )
            _write_json(
                target / "topology-source-fusion.json",
                {
                    "evidence": ordered_evidence,
                    "changed_jobs": fusion.changed_jobs,
                    "spans": fusion.provenance,
                    "cache_metrics": fusion.cache_metrics,
                },
            )
            manifest_path = target / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "ocr_wall_seconds": patched.ocr_seconds,
                    "ocr_work_seconds": merged_metrics.get(
                        "ocr_work_seconds", patched.ocr_seconds
                    ),
                    "cache_requests": merged_metrics.get("requests", 0),
                    "cache_hits": merged_metrics.get("hits", 0),
                    "cache_misses": merged_metrics.get("misses", 0),
                    "exact_duplicate_calls": merged_metrics.get(
                        "exact_duplicate_calls_avoided", 0
                    ),
                    "topology_source_fusion": {
                        "changed_jobs": fusion.changed_jobs,
                        "observed_spans": len(fusion.provenance),
                        "transform": "contextual/composite",
                    },
                }
            )
            _write_json(manifest_path, manifest)

    _write_json(output / "cache-metrics.json", content_cache.snapshot())
    status = "failed" if stage_issues else "complete"
    _stage_manifest(
        output,
        stage="ocr-blocks",
        input_path=input_path,
        status=status,
        artifacts=policy_count,
        issues=stage_issues,
    )
    print(output)
    return 3 if stage_issues else 0


def _plan_diagnostic_int(
    plan: Any,
    prefix: str,
) -> int | None:
    for item in plan.diagnostics:
        if not item.startswith(prefix):
            continue
        value = item.removeprefix(prefix)
        if value.isdigit():
            return int(value)
    return None


def _table_whole_object_ocr_reason(
    stored: recognition.StoredObject,
    separated: recognition.SeparatedBlocks,
) -> str | None:
    if stored.source_kind != "table" or separated.policy != "matrix-orxor":
        return None
    logical_rows = _plan_diagnostic_int(
        separated.plan,
        "matrix-logical-row-bands=",
    )
    logical_columns = _plan_diagnostic_int(
        separated.plan,
        "matrix-logical-column-bands=",
    )
    if (
        logical_rows is not None
        and logical_columns is not None
        and logical_rows <= 2
        and logical_columns <= 2
    ):
        return "table-at-most-2x2"

    raw_rows = stored.matrix_payload.get("rows")
    if not isinstance(raw_rows, list) or logical_columns is None:
        return None
    maximum_payload_cells = 0
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        segments = row.get("segments")
        if not isinstance(segments, list):
            continue
        payload_cells = sum(
            isinstance(segment, Mapping)
            and isinstance(segment.get("source_segment_ids"), list)
            and bool(segment["source_segment_ids"])
            for segment in segments
        )
        maximum_payload_cells = max(maximum_payload_cells, payload_cells)
    if logical_columns <= 2 and maximum_payload_cells < 2:
        return "narrow-table-without-vertical-payload-split"
    return None


def _clean_table_cell(value: str) -> str:
    tokens = tuple(
        token
        for token in value.replace("\n", " ").split()
        if token not in {"|", "¦", "│"}
    )
    return " ".join(tokens).strip(" |,.:;")


def _render_table_rows(
    rows: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], str] | None:
    populated = tuple(
        row for row in rows if any(cell.strip() for cell in row)
    )
    if len(populated) < 2:
        return None
    markdown = [
        "| "
        + " | ".join(
            format_markdown_table_cell(cell)
            for cell in row
        )
        + " |"
        for row in populated
    ]
    markdown.insert(
        1,
        "| " + " | ".join("---" for _cell in populated[0]) + " |",
    )
    segment_lines = tuple(
        f"table-row-{index:06d}\t" + "\t".join(row)
        for index, row in enumerate(populated)
    )
    return segment_lines, "\n".join(markdown) + "\n"


def _recover_whole_table_rows(
    block_ocr: recognition.BlockOcrResult,
) -> tuple[
    tuple[str, ...],
    str,
    tuple[tuple[str, ...], ...],
    dict[tuple[int, int], tuple[str, ...]],
] | None:
    plan = block_ocr.separated.plan
    expected_rows = _plan_diagnostic_int(
        plan,
        "matrix-logical-row-bands=",
    )
    expected_columns = _plan_diagnostic_int(
        plan,
        "matrix-logical-column-bands=",
    )
    if (
        block_ocr.separated.policy != "whole-object"
        or expected_rows is None
        or expected_columns != 2
    ):
        return None
    crops = {
        crop.block_id: crop for crop in block_ocr.separated.crops
    }
    candidates = [
        job
        for job in block_ocr.queue.jobs
        if (
            job.status.value == "complete"
            and job.output is not None
            and job.block_id in crops
        )
    ]
    if not candidates:
        return None
    job = max(
        candidates,
        key=lambda item: (
            sum(word.confidence for word in item.output.words)
            / max(1, len(item.output.words)),
            len(item.output.text),
        ),
    )
    assert job.output is not None
    crop = crops[job.block_id]
    words = [
        (
            word,
            crop.bbox.left + word.bbox.left,
            crop.bbox.top + word.bbox.top,
            crop.bbox.left + word.bbox.right,
            crop.bbox.top + word.bbox.bottom,
        )
        for word in job.output.words
        if word.text.strip()
    ]
    if not words:
        return None

    visual_rows: list[list[tuple[Any, int, int, int, int]]] = []
    for word in sorted(
        words,
        key=lambda item: (
            (item[2] + item[4]) / 2,
            item[1],
        ),
    ):
        best_index: int | None = None
        best_overlap = 0
        for index, row_words in enumerate(visual_rows):
            row_top = min(item[2] for item in row_words)
            row_bottom = max(item[4] for item in row_words)
            overlap = min(row_bottom, word[4]) - max(row_top, word[2])
            if overlap > best_overlap:
                best_index = index
                best_overlap = overlap
        if best_index is None:
            visual_rows.append([word])
        else:
            visual_rows[best_index].append(word)

    def row_center(row_words: list[tuple[Any, int, int, int, int]]) -> float:
        return sum((item[2] + item[4]) / 2 for item in row_words) / len(
            row_words
        )

    visual_rows.sort(key=row_center)
    while len(visual_rows) > expected_rows:
        merge_index = min(
            range(len(visual_rows) - 1),
            key=lambda index: (
                row_center(visual_rows[index + 1])
                - row_center(visual_rows[index])
            ),
        )
        visual_rows[merge_index].extend(visual_rows.pop(merge_index + 1))
    if len(visual_rows) != expected_rows:
        return None

    split_candidates: list[tuple[float, int]] = []
    for row_words in visual_rows:
        ordered = sorted(row_words, key=lambda item: item[1])
        gaps = [
            (next_word[1] - previous[3], previous, next_word)
            for previous, next_word in zip(ordered, ordered[1:])
        ]
        if not gaps:
            continue
        gap, previous, next_word = max(gaps, key=lambda item: item[0])
        row_height = max(item[4] - item[2] for item in ordered)
        if gap >= max(2, row_height):
            split_candidates.append(
                ((previous[3] + next_word[1]) / 2, gap)
            )
    if len(split_candidates) < 2:
        return None
    boundaries = sorted(item[0] for item in split_candidates)
    boundary = boundaries[len(boundaries) // 2]

    rows: list[tuple[str, str]] = []
    for row_words in visual_rows:
        ordered = sorted(row_words, key=lambda item: item[1])
        left = _clean_table_cell(
            " ".join(
                item[0].text
                for item in ordered
                if (item[1] + item[3]) / 2 < boundary
            )
        )
        right = _clean_table_cell(
            " ".join(
                item[0].text
                for item in ordered
                if (item[1] + item[3]) / 2 >= boundary
            )
        )
        rows.append((left, right))
    if sum(bool(left and right) for left, right in rows) < 2:
        return None
    recovered_rows = tuple(rows)
    rendered = _render_table_rows(recovered_rows)
    if rendered is None:
        return None
    segment_lines, result_text = rendered
    return (
        segment_lines,
        result_text,
        recovered_rows,
        {
            (row_index, column_index): (job.job_id,)
            for row_index, row in enumerate(recovered_rows)
            for column_index, text in enumerate(row)
            if text.strip()
        },
    )


def _recover_table_rows(
    stored: recognition.StoredObject,
    block_ocr: recognition.BlockOcrResult,
) -> tuple[Any, ...] | None:
    whole_table = _recover_whole_table_rows(block_ocr)
    if whole_table is not None:
        return whole_table
    raw_rows = stored.matrix_payload.get("rows")
    if not isinstance(raw_rows, list):
        return None
    row_ranges: list[tuple[int, int]] = []
    cell_ranges: list[dict[int, tuple[int, int]]] = []
    column_indexes: set[int] = set()
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("matrix_y", row.get("y"))
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(type(item) is int for item in value)
            and value[0] < value[1]
        ):
            row_ranges.append((value[0], value[1]))
        else:
            continue
        row_cells: dict[int, tuple[int, int]] = {}
        segments = row.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, Mapping):
                    continue
                column = segment.get("column")
                horizontal = segment.get("matrix_x", segment.get("x"))
                if (
                    type(column) is int
                    and isinstance(horizontal, list)
                    and len(horizontal) == 2
                    and all(type(item) is int for item in horizontal)
                    and horizontal[0] < horizontal[1]
                ):
                    row_cells[column] = (horizontal[0], horizontal[1])
                    column_indexes.add(column)
        cell_ranges.append(row_cells)
    if len(row_ranges) < 2:
        return None
    columns = tuple(sorted(column_indexes))
    if len(columns) < 2 or len(cell_ranges) != len(row_ranges):
        return None

    crops = {
        crop.block_id: crop for crop in block_ocr.separated.crops
    }
    blocks = {
        block.block_id: block for block in block_ocr.separated.plan.blocks
    }
    candidates: dict[
        tuple[int, int],
        list[tuple[str, float, str, str]],
    ] = defaultdict(list)
    for job in block_ocr.queue.jobs:
        if job.status.value != "complete" or job.output is None:
            continue
        crop = crops.get(job.block_id)
        block = blocks.get(job.block_id)
        if crop is None or block is None:
            continue
        grouped: dict[tuple[int, str], list[Any]] = defaultdict(list)
        for word in job.output.words:
            center_x = block.bbox.left + (
                word.bbox.left + word.bbox.right
            ) / 2
            center_y = block.bbox.top + (
                word.bbox.top + word.bbox.bottom
            ) / 2
            row_index = next(
                (
                    index
                    for index, (top, bottom) in enumerate(row_ranges)
                    if top <= center_y < bottom
                ),
                None,
            )
            if row_index is None:
                continue
            column = next(
                (
                    index
                    for index, (left, right) in cell_ranges[
                        row_index
                    ].items()
                    if left <= center_x < right
                ),
                None,
            )
            if column is None:
                continue
            grouped[(row_index, column)].append(word)
        for key, words in grouped.items():
            text = _clean_table_cell(" ".join(item.text for item in words))
            if not text:
                continue
            confidence = sum(item.confidence for item in words) / len(words)
            candidates[key].append(
                (text, confidence, job.block_id, job.job_id)
            )

    selected: dict[tuple[int, int], str] = {}
    selected_job_ids: dict[tuple[int, int], tuple[str, ...]] = {}
    for key, observations in candidates.items():
        by_text: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
        for text, confidence, block_id, job_id in observations:
            by_text[text].append((confidence, block_id, job_id))
        selected_text = max(
            by_text,
            key=lambda text: (
                len(
                    {
                        block_id
                        for _confidence, block_id, _job_id in by_text[text]
                    }
                ),
                len(by_text[text]),
                sum(
                    confidence
                    for confidence, _block_id, _job_id in by_text[text]
                )
                / len(by_text[text]),
                len(text),
                text,
            ),
        )
        selected[key] = selected_text
        selected_job_ids[key] = tuple(
            sorted(
                {
                    job_id
                    for _confidence, _block_id, job_id in by_text[
                        selected_text
                    ]
                }
            )
        )

    sparse_rows = tuple(
        tuple(selected.get((row_index, column), "") for column in columns)
        for row_index in range(len(row_ranges))
    )
    rendered = _render_table_rows(sparse_rows)
    if rendered is None:
        return None
    segment_lines, result_text = rendered
    return segment_lines, result_text, sparse_rows, selected_job_ids


def _normalize_table_recovery(
    recovered: tuple[Any, ...],
) -> tuple[
    tuple[str, ...],
    str,
    tuple[tuple[str, ...], ...],
    dict[tuple[int, int], tuple[str, ...]],
]:
    if len(recovered) == 4:
        segment_lines, result_text, rows, raw_cell_job_ids = recovered
        if not isinstance(raw_cell_job_ids, Mapping):
            raise ValueError("table recovery cell evidence must be a mapping")
        return (
            segment_lines,
            result_text,
            rows,
            {
                coordinate: tuple(sorted(set(job_ids)))
                for coordinate, job_ids in raw_cell_job_ids.items()
            },
        )
    if len(recovered) == 3:
        segment_lines, result_text, rows = recovered
        return segment_lines, result_text, rows, {}
    if len(recovered) != 2:
        raise ValueError(
            f"unsupported table recovery handoff arity {len(recovered)}"
        )
    segment_lines, result_text = recovered
    rows = tuple(
        tuple(line.split("\t")[1:])
        for line in segment_lines
        if line.startswith("table-row-") and "\t" in line
    )
    if len(rows) != len(segment_lines) or any(not row for row in rows):
        raise ValueError(
            "legacy table recovery handoff cannot reconstruct row cells"
        )
    return segment_lines, result_text, rows, {}


def _table_ocr_job_ids_by_source_segment(
    block_ocr: recognition.BlockOcrResult,
) -> dict[str, tuple[str, ...]]:
    blocks = {
        block.block_id: block for block in block_ocr.separated.plan.blocks
    }
    job_ids_by_source_segment: dict[str, set[str]] = defaultdict(set)
    for job in block_ocr.queue.jobs:
        if job.status.value != "complete" or job.output is None:
            continue
        block = blocks.get(job.block_id)
        if block is None:
            continue
        for source_segment_id in block.segment_ids:
            job_ids_by_source_segment[source_segment_id].add(job.job_id)
    return {
        source_segment_id: tuple(sorted(job_ids))
        for source_segment_id, job_ids in job_ids_by_source_segment.items()
    }


def _table_handoff_segments(
    stored: recognition.StoredObject,
    rows: tuple[tuple[str, ...], ...],
    *,
    ocr_job_ids_by_source_segment: Mapping[
        str, Iterable[str]
    ] | None = None,
    ocr_job_ids_by_cell: Mapping[
        tuple[int, int], Iterable[str]
    ] | None = None,
) -> list[dict[str, Any]]:
    logical_row_count = len(rows)
    logical_column_count = max((len(row) for row in rows), default=0)
    physical_rows = tuple(sorted({cell.row for cell in stored.matrix.cells}))
    physical_columns = tuple(
        sorted({cell.column for cell in stored.matrix.cells})
    )

    def logical_axis(
        physical: tuple[int, ...],
        logical_count: int,
    ) -> dict[int, int]:
        if len(physical) == logical_count:
            return {
                coordinate: index
                for index, coordinate in enumerate(physical)
            }
        if all(0 <= coordinate < logical_count for coordinate in physical):
            return {coordinate: coordinate for coordinate in physical}
        return {
            coordinate: index
            for index, coordinate in enumerate(physical[:logical_count])
        }

    logical_row_by_physical = logical_axis(
        physical_rows,
        logical_row_count,
    )
    logical_column_by_physical = logical_axis(
        physical_columns,
        logical_column_count,
    )
    source_ids_by_cell: dict[tuple[int, int], list[str]] = defaultdict(list)
    for cell in stored.matrix.cells:
        logical_row = logical_row_by_physical.get(cell.row)
        logical_column = logical_column_by_physical.get(cell.column)
        if logical_row is None or logical_column is None:
            continue
        source_ids_by_cell[(logical_row, logical_column)].append(
            cell.segment_id
        )

    materialized_coordinates = set(source_ids_by_cell)
    for row_index, row in enumerate(rows):
        materialized_coordinates.update(
            (row_index, column_index)
            for column_index, text in enumerate(row)
            if text.strip()
        )

    segments: list[dict[str, Any]] = []
    source_jobs = ocr_job_ids_by_source_segment or {}
    cell_jobs = ocr_job_ids_by_cell or {}
    for row_index, column_index in sorted(materialized_coordinates):
        if not (
            0 <= row_index < logical_row_count
            and 0 <= column_index < logical_column_count
        ):
            continue
        text = (
            rows[row_index][column_index].strip()
            if column_index < len(rows[row_index])
            else ""
        )
        source_segment_ids = tuple(
            sorted(set(source_ids_by_cell[(row_index, column_index)]))
        )
        relevant_job_ids = set(cell_jobs.get((row_index, column_index), ()))
        for source_segment_id in source_segment_ids:
            relevant_job_ids.update(source_jobs.get(source_segment_id, ()))
        segment: dict[str, Any] = {
            "segment_id": (
                f"{stored.source_object_id}-cell-"
                f"{row_index:06d}-{row_index + 1:06d}-"
                f"{column_index:06d}-{column_index + 1:06d}"
            ),
            "source_segment_ids": source_segment_ids,
            "topology": {
                "object_id": stored.source_object_id,
                "object_kind": "table",
                "row": row_index,
                "column": column_index,
                "row_span": 1,
                "column_span": 1,
            },
            "text": text,
        }
        if relevant_job_ids:
            segment["evidence"] = {
                "ocr_job_ids": tuple(sorted(relevant_job_ids)),
            }
        segments.append(segment)
    return segments


def _handoff_object_layout(
    stored: recognition.StoredObject,
    segments: list[dict[str, Any]],
    *,
    rows: tuple[tuple[str, ...], ...] | None = None,
) -> dict[str, Any]:
    if rows is not None:
        object_kind = "table"
        logical_row_count = len(rows)
        logical_column_count = max((len(row) for row in rows), default=0)
    else:
        topologies = tuple(
            segment["topology"]
            for segment in segments
            if isinstance(segment.get("topology"), Mapping)
        )
        object_kind = (
            str(topologies[0]["object_kind"])
            if topologies
            else "table" if stored.source_kind == "table" else "paragraph"
        )
        logical_row_count = max(
            (
                int(topology["row"]) + int(topology["row_span"])
                for topology in topologies
            ),
            default=0,
        )
        logical_column_count = max(
            (
                int(topology["column"]) + int(topology["column_span"])
                for topology in topologies
            ),
            default=0,
        )
    return {
        "object_id": stored.source_object_id,
        "object_kind": object_kind,
        "logical_row_count": logical_row_count,
        "logical_column_count": logical_column_count,
    }


def _segment_topology_handoff_payload(
    segments: Iterable[dict[str, Any]],
    object_layouts: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    materialized = list(segments)
    ocr_job_ids = tuple(
        sorted(
            {
                str(job_id)
                for segment in materialized
                for job_id in (
                    segment.get("evidence", {}).get("ocr_job_ids", ())
                    if isinstance(segment.get("evidence"), Mapping)
                    else ()
                )
            }
        )
    )
    job_ref_by_id = {
        job_id: index for index, job_id in enumerate(ocr_job_ids)
    }
    indexed_segments: list[dict[str, Any]] = []
    for segment in materialized:
        indexed = {
            key: value for key, value in segment.items() if key != "evidence"
        }
        evidence = segment.get("evidence")
        if isinstance(evidence, Mapping):
            refs = tuple(
                sorted(
                    {
                        job_ref_by_id[str(job_id)]
                        for job_id in evidence.get("ocr_job_ids", ())
                    }
                )
            )
            if refs:
                indexed["evidence"] = {"ocr_job_refs": refs}
        indexed_segments.append(indexed)
    return {
        "schema": "ittm.segment-topology-handoff/v1",
        "source": "raster_geometry",
        "object_layouts": tuple(object_layouts),
        "evidence_index": {"ocr_job_ids": ocr_job_ids},
        "segments": indexed_segments,
    }


def _text_handoff_segments(
    stored: recognition.StoredObject,
    run: recognition.PolicyRun,
) -> list[dict[str, Any]]:
    text = run.result_text.strip()
    if not text:
        return []
    return [
        {
            "segment_id": (
                f"{stored.source_object_id}-{run.policy}-segment-000000"
            ),
            "source_segment_ids": tuple(
                segment.segment_id for segment in stored.segments
            ),
            "topology": {
                "object_id": stored.source_object_id,
                "object_kind": "paragraph",
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
            },
            "text": text,
        }
    ]


def _run_get_segment(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    input_path = args.input.resolve(strict=True)
    _prepare_output(output, replace=args.replace)
    state_directories = _state_directories(input_path)
    if not state_directories:
        raise ValueError(f"no ocr-blocks state found in {input_path}")

    stage_issues: list[str] = []
    policy_outcomes: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    stored_by_object: dict[str, recognition.StoredObject] = {}
    runs_by_object: dict[
        str,
        dict[str, recognition.PolicyRun],
    ] = defaultdict(dict)
    handoff_by_object: dict[
        str,
        dict[str, list[dict[str, Any]]],
    ] = defaultdict(dict)
    handoff_layout_by_object: dict[str, dict[str, Any]] = {}
    policy_count = 0
    for state_directory in state_directories:
        state = _read_state(state_directory)
        if not (
            isinstance(state, tuple)
            and len(state) == 2
            and isinstance(state[0], recognition.StoredObject)
            and isinstance(state[1], recognition.BlockOcrResult)
        ):
            continue
        stored, block_ocr = state
        run = recognition.get_segments(stored, block_ocr)
        table_recovered = False
        table_rows: tuple[tuple[str, ...], ...] | None = None
        table_cell_job_ids: dict[
            tuple[int, int], tuple[str, ...]
        ] = {}
        if stored.source_kind == "table":
            recovered = _recover_table_rows(stored, block_ocr)
            if recovered is not None:
                (
                    segment_lines,
                    result_text,
                    table_rows,
                    table_cell_job_ids,
                ) = (
                    _normalize_table_recovery(recovered)
                )
                run = dataclasses.replace(
                    run,
                    segment_lines=segment_lines,
                    result_text=result_text,
                )
                table_recovered = True
        elif run.policy == "whole-object" and run.result_text.strip():
            run = dataclasses.replace(
                run,
                unresolved_units=0,
                fusion_status="complete",
                fusion_error="",
            )
        object_stem = _stored_stem(stored)
        stored_by_object[object_stem] = stored
        runs_by_object[object_stem][run.policy] = run
        handoff_segments = (
            _table_handoff_segments(
                stored,
                table_rows,
                ocr_job_ids_by_source_segment=(
                    _table_ocr_job_ids_by_source_segment(block_ocr)
                ),
                ocr_job_ids_by_cell=table_cell_job_ids,
            )
            if table_rows is not None
            else _text_handoff_segments(stored, run)
        )
        handoff_by_object[object_stem][run.policy] = handoff_segments
        handoff_layout_by_object[object_stem] = _handoff_object_layout(
            stored,
            handoff_segments,
            rows=table_rows,
        )
        projected_source_segment_ids = {
            segment_id
            for segment in handoff_segments
            for segment_id in segment.get("source_segment_ids", ())
        }
        expected_source_segment_ids = {
            segment.segment_id for segment in stored.segments
        }
        projection_missing_segment_ids = tuple(
            sorted(
                expected_source_segment_ids
                - projected_source_segment_ids
            )
        )
        projection_complete = (
            table_recovered
            and not projection_missing_segment_ids
            and all(
                segment.get("segment_id")
                and (
                    segment.get("source_segment_ids")
                    or (
                        isinstance(segment.get("evidence"), Mapping)
                        and segment["evidence"].get("ocr_job_ids")
                    )
                )
                for segment in handoff_segments
                if str(segment.get("text", "")).strip()
            )
        )
        target = output / "objects" / object_stem / run.policy
        target.mkdir(parents=True, exist_ok=False)
        _write_state(target, (stored, run))
        _write_json(
            target / "segment-topology-handoff.json",
            _segment_topology_handoff_payload(
                handoff_segments,
                (handoff_layout_by_object[object_stem],),
            ),
        )
        _write_text(
            target / "segments.txt",
            "\n".join(run.segment_lines)
            + ("\n" if run.segment_lines else ""),
        )
        _write_text(
            target / "result.txt",
            run.result_text,
            render_markdown=True,
        )
        issues: list[str] = []
        if table_recovered:
            if projection_missing_segment_ids:
                issues.append(
                    "table cell projection missing segments: "
                    + ",".join(projection_missing_segment_ids)
                )
            if not projection_complete and not projection_missing_segment_ids:
                issues.append(
                    "nonempty table cell projection lacks segment evidence"
                )
        else:
            if run.fusion_status != "complete":
                issues.append(f"fusion status is {run.fusion_status}")
            if run.fusion_error:
                issues.append(run.fusion_error)
            if run.unresolved_units:
                issues.append(f"unresolved units: {run.unresolved_units}")
        status = "failed" if issues else "complete"
        policy_outcomes[object_stem].append((run.policy, not issues))
        payload = recognition._policy_payload(run)
        payload.update(
            {
                "schema": "debug-get-segment-v1",
                "stage": "get-segment",
                "status": status,
                "table_cell_projection": table_recovered,
                "table_cell_projection_complete": projection_complete,
                "table_cell_projection_source_segments": len(
                    projected_source_segment_ids
                ),
                "table_cell_projection_missing_segment_ids": (
                    projection_missing_segment_ids
                ),
                "membership_fusion_status": run.fusion_status,
                "issues": issues,
            }
        )
        _write_json(target / "manifest.json", payload)
        policy_count += 1

    for object_stem, outcomes in sorted(policy_outcomes.items()):
        if not any(complete for _policy, complete in outcomes):
            stage_issues.append(
                f"{object_stem}: no get-segment policy completed"
            )
    selected_handoff_segments: list[dict[str, Any]] = []
    selected_handoff_layouts: list[dict[str, Any]] = []
    selected_policies: dict[str, str] = {}
    for object_stem, runs in sorted(runs_by_object.items()):
        selected_policy = _canonical_policy(
            stored_by_object[object_stem],
            runs,
        )
        selected_policies[object_stem] = selected_policy
        selected_handoff_segments.extend(
            handoff_by_object[object_stem][selected_policy]
        )
        selected_handoff_layouts.append(
            handoff_layout_by_object[object_stem]
        )
    _write_json(
        output / "segment-topology-handoff.json",
        _segment_topology_handoff_payload(
            selected_handoff_segments,
            selected_handoff_layouts,
        ),
    )
    _write_json(
        output / "segment-topology-selection.json",
        {
            "schema": "debug-segment-topology-selection-v1",
            "selected_policies": selected_policies,
        },
    )
    status = "failed" if stage_issues else "complete"
    _stage_manifest(
        output,
        stage="get-segment",
        input_path=input_path,
        status=status,
        artifacts=policy_count,
        issues=stage_issues,
    )
    print(output)
    return 3 if stage_issues else 0


def _canonical_policy(
    stored: recognition.StoredObject,
    runs: Mapping[str, recognition.PolicyRun],
) -> str:
    if stored.source_kind == "table" and "matrix-orxor" in runs:
        return "matrix-orxor"
    preferred = (
        ("whole-object", "line-windows")
        if stored.source_kind == "list"
        else ("line-windows", "whole-object")
    )
    for policy in preferred:
        run = runs.get(policy)
        if (
            run is not None
            and run.fusion_status == "complete"
            and not run.unresolved_units
        ):
            return policy
    for policy in preferred:
        if policy in runs:
            return policy
    return sorted(runs)[0]


def _attach_external_table_header(
    header_text: str,
    table_text: str,
) -> str | None:
    header_lines = tuple(
        line.strip()
        for line in header_text.splitlines()
        if line.strip()
    )
    table_lines = tuple(
        line.strip()
        for line in table_text.splitlines()
        if line.strip()
    )
    if len(header_lines) != 1 or len(table_lines) < 2:
        return None
    first_row = table_lines[0]
    separator = table_lines[1]
    if not (
        first_row.startswith("|")
        and first_row.endswith("|")
        and separator.startswith("|")
        and separator.endswith("|")
    ):
        return None
    first_cells = tuple(
        cell.strip() for cell in first_row[1:-1].split("|")
    )
    separator_cells = tuple(
        cell.strip() for cell in separator[1:-1].split("|")
    )
    header_cells = tuple(header_lines[0].split())
    if (
        len(first_cells) < 2
        or len(header_cells) != len(first_cells)
        or len(separator_cells) != len(first_cells)
        or any(cell.strip("-: ") for cell in separator_cells)
    ):
        return None
    header_row = "| " + " | ".join(header_cells) + " |"
    return "\n".join(
        (header_row, separator, first_row, *table_lines[2:])
    ) + "\n"


def _node_package_root(
    package_name: str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    local_package = repository_root / "node_modules" / package_name / "package.json"
    if local_package.is_file():
        return repository_root

    completed = subprocess.run(
        (
            "git",
            "-C",
            str(repository_root),
            "rev-parse",
            "--git-common-dir",
        ),
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    checked = [local_package]
    if completed.returncode == 0 and completed.stdout.strip():
        common_dir = Path(completed.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repository_root / common_dir
        common_root = common_dir.resolve().parent
        common_package = (
            common_root / "node_modules" / package_name / "package.json"
        )
        checked.append(common_package)
        if common_package.is_file():
            return common_root

    detail = (
        completed.stderr.strip()
        if completed.returncode
        else "package was not installed"
    )
    raise RuntimeError(
        f"required Node package {package_name!r} is unavailable; checked "
        + ", ".join(str(path) for path in checked)
        + f" ({detail})"
    )


def _run_generate_object(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    input_path = args.input.resolve(strict=True)
    _prepare_output(output, replace=args.replace)
    handoff_path = (
        input_path
        if input_path.is_file()
        else input_path / "segment-topology-handoff.json"
    )
    if handoff_path.is_file():
        node_package_root = _node_package_root("tsx")
        command = (
            "node",
            "--import",
            "tsx",
            str(
                REPOSITORY_ROOT
                / "scripts"
                / "debug"
                / "assemble-segment-topology-handoff.ts"
            ),
            str(handoff_path),
        )
        completed = subprocess.run(
            command,
            cwd=node_package_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise RuntimeError(
                "shared segment assembler failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        artifact = json.loads(completed.stdout)
        if not isinstance(artifact, dict):
            raise ValueError("shared segment assembler returned no object")
        objects = artifact.get("objects")
        if not isinstance(objects, list):
            raise ValueError("shared segment assembler returned no objects")
        result_text = artifact.get("markdown")
        if not isinstance(result_text, str):
            raise ValueError("shared segment assembler returned no Markdown")
        _write_json(output / "assembly.json", artifact)
        _write_text(
            output / "result.txt",
            result_text + ("\n" if result_text else ""),
            render_markdown=True,
        )
        for assembled in objects:
            if not isinstance(assembled, dict):
                raise ValueError("shared assembler object must be an object")
            object_id = assembled.get("object_id")
            kind = assembled.get("kind")
            markdown = assembled.get("markdown")
            if not all(
                isinstance(value, str)
                for value in (object_id, kind, markdown)
            ):
                raise ValueError("shared assembler object is incomplete")
            object_stem = f"{_safe_name(object_id)}-{_safe_name(kind)}"
            target = output / "objects" / object_stem
            target.mkdir(parents=True, exist_ok=False)
            _write_text(
                target / f"{object_stem}.txt",
                markdown + ("\n" if markdown else ""),
                render_markdown=True,
            )
            _write_json(
                target / "manifest.json",
                {
                    "schema": "debug-generate-object-v2",
                    "stage": "generate-object",
                    "status": "complete",
                    "object_id": object_id,
                    "object_kind": kind,
                    "assembler": "shared-segment-topology",
                    "issues": (),
                },
            )
        stage_issues: list[str] = []
        comparison: dict[str, Any] | None = None
        if args.reference is not None:
            reference_text = args.reference.resolve(strict=True).read_text(
                encoding="utf-8"
            )
            comparison = recognition._metric(reference_text, result_text)
            _write_json(output / "comparison.json", comparison)
            if comparison.get("accuracy_percent") != 100.0:
                stage_issues.append(
                    "result differs from reference: "
                    f"{comparison.get('accuracy_percent')}%"
                )
        status = "failed" if stage_issues else "complete"
        _stage_manifest(
            output,
            stage="generate-object",
            input_path=handoff_path,
            status=status,
            artifacts=len(objects),
            issues=stage_issues,
        )
        print(output)
        return 3 if stage_issues else 0
    state_directories = _state_directories(input_path)
    if not state_directories:
        raise ValueError(f"no get-segment state found in {input_path}")

    grouped: dict[
        str,
        list[tuple[recognition.StoredObject, recognition.PolicyRun]],
    ] = defaultdict(list)
    for state_directory in state_directories:
        state = _read_state(state_directory)
        if not (
            isinstance(state, tuple)
            and len(state) == 2
            and isinstance(state[0], recognition.StoredObject)
            and isinstance(state[1], recognition.PolicyRun)
        ):
            continue
        grouped[_stored_stem(state[0])].append((state[0], state[1]))

    if not grouped:
        raise ValueError(f"no compatible get-segment state found in {input_path}")

    object_results: list[tuple[str, str, str]] = []
    stage_issues: list[str] = []
    for object_stem, values in sorted(grouped.items()):
        stored = values[0][0]
        runs = {run.policy: run for _, run in values}
        selected_policy = _canonical_policy(stored, runs)
        selected = runs[selected_policy]
        selected_text = selected.result_text
        attached_header_object_id: str | None = None
        if stored.source_kind == "table" and object_results:
            (
                previous_object_id,
                previous_kind,
                previous_text,
            ) = object_results[-1]
            if previous_kind == "paragraph":
                assembled = _attach_external_table_header(
                    previous_text,
                    selected_text,
                )
                if assembled is not None:
                    object_results.pop()
                    selected_text = assembled
                    attached_header_object_id = previous_object_id
        target = output / "objects" / object_stem
        target.mkdir(parents=True, exist_ok=False)
        for policy, run in sorted(runs.items()):
            _write_text(
                target / f"{policy}.txt",
                (
                    selected_text
                    if policy == selected_policy
                    else run.result_text
                ),
                render_markdown=True,
            )
            _write_json(
                target / f"{policy}.json",
                recognition._policy_payload(run),
            )
        _write_text(
            target / f"{object_stem}.txt",
            selected_text,
            render_markdown=True,
        )
        issues: list[str] = []
        if selected.fusion_status != "complete":
            issues.append(
                f"selected policy fusion status is {selected.fusion_status}"
            )
        if selected.unresolved_units:
            issues.append(
                f"selected policy unresolved units: {selected.unresolved_units}"
            )
        stage_issues.extend(f"{object_stem}: {issue}" for issue in issues)
        _write_json(
            target / "manifest.json",
            {
                "schema": "debug-generate-object-v1",
                "stage": "generate-object",
                "status": "failed" if issues else "complete",
                "object_id": stored.source_object_id,
                "object_kind": stored.source_kind,
                "available_policies": tuple(sorted(runs)),
                "selected_policy": selected_policy,
                "attached_header_object_id": attached_header_object_id,
                "issues": issues,
            },
        )
        object_results.append(
            (stored.source_object_id, stored.source_kind, selected_text)
        )

    result_text = "\n\n".join(
        text.strip()
        for _, _kind, text in sorted(object_results)
        if text.strip()
    )
    if result_text:
        result_text += "\n"
    _write_text(
        output / "result.txt",
        result_text,
        render_markdown=True,
    )

    comparison: dict[str, Any] | None = None
    if args.reference is not None:
        reference_text = args.reference.resolve(strict=True).read_text(
            encoding="utf-8"
        )
        comparison = recognition._metric(reference_text, result_text)
        accuracy = comparison.get("accuracy_percent")
        if accuracy != 100.0:
            stage_issues.append(
                f"reference accuracy is {accuracy!r}, expected 100.0"
            )
        _write_json(output / "comparison.json", comparison)

    status = "failed" if stage_issues else "complete"
    _stage_manifest(
        output,
        stage="generate-object",
        input_path=input_path,
        status=status,
        artifacts=len(object_results),
        issues=stage_issues,
    )
    if comparison is not None:
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["comparison"] = comparison
        _write_json(manifest_path, manifest)
    print(output)
    return 3 if stage_issues else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one persisted object-recognition debug stage. "
            "Inputs may be injected from another trusted run."
        )
    )
    parser.add_argument("--stage", required=True, choices=OBJECT_STAGES)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--geometry-dir", type=Path)
    parser.add_argument("--objects-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--tesseract-executable", default="tesseract")
    parser.add_argument("--tessdata", type=Path)
    parser.add_argument(
        "--languages",
        default="eng,chi_sim,rus",
        help="comma-separated Tesseract language ids",
    )
    parser.add_argument("--tesseract-workers", type=int, default=4)
    parser.add_argument("--tesseract-psm", type=int, choices=(4, 6), default=6)
    parser.add_argument(
        "--paragraph-list-ab",
        action="store_true",
        help="always run both paragraph/list policies for diagnostics",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage != "find-object" and args.input is None:
        raise ValueError(f"{args.stage} requires --input")
    runners = {
        "find-object": _run_find_object,
        "separate-block": _run_separate_block,
        "ocr-blocks": _run_ocr_blocks,
        "get-segment": _run_get_segment,
        "generate-object": _run_generate_object,
    }
    return runners[args.stage](args)


if __name__ == "__main__":
    raise SystemExit(main())
