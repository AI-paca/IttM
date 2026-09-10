#!/usr/bin/env python3
"""Export trusted Python05 state, never rerun planning, rendering, fusion or OCR.

Run with the a772cb1d Python dependencies. Example:
  python3 -B export_python_ocr.py --trust-pickle --input ITEM/05-ocr-blocks \
      --source-stage03 ITEM/03-find-object --output NEW_DIRECTORY

The output contains 05-ocr-blocks/{checkpoint.json,input.json,blocks/checkpoint.json}.
Legacy ABI-v1 records remain source-backed. Lossless JSON extensions (matrix_cells,
object_layouts, policy, membership_units, original confidence and job identity)
must also be consumed for full Python get-segment parity. No fake source IDs are
assigned to empty cells. Source-stage03 is provenance, not an export dependency.
Only policy names, never stage06 recognized content, are read from selection JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
DEFAULT_PYTHON_ROOT = Path(
    "/home/alpaca/GitHub/IttM-engine-worktrees/topology-splay-locked-20260818"
)
KINDS = {"paragraph": 0, "list": 1, "table": 2, "unknown": 3}


def numeric_id(value):
    return int(value.rsplit("-", 1)[1])


def interval(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(x) is int for x in value) and value[0] < value[1])


def page_box(box, offset):
    values = box.as_tuple() if hasattr(box, "as_tuple") else box
    return [int(x) + offset[i % 2] for i, x in enumerate(values)]


def matrix_cells(stored):
    """Serialize explicit matrix slots in Python recovery traversal order."""
    offset = tuple(stored.record["bbox"][:2])
    cells, columns, row_index = [], set(), 0
    rows = stored.matrix_payload.get("rows", [])
    maximum_column = 0
    for row in rows:
        y = row.get("matrix_y", row.get("y"))
        maximum_column = max(maximum_column, int(
            (row.get("null_tail") or {}).get("through_column_exclusive", 0)))
        if not interval(y):
            continue
        for slot in row.get("segments", []):
            x = slot.get("matrix_x", slot.get("x"))
            column = slot.get("column")
            if type(column) is not int or not interval(x):
                continue
            columns.add(column)
            maximum_column = max(maximum_column, column + 1)
            local = [x[0], y[0], x[1], y[1]]
            cells.append({
                "row": row_index, "physical_row": int(row["row"]),
                "column": column, "row_span": 1, "column_span": 1,
                "matrix_x": list(x), "matrix_y": list(y),
                "bbox_object": local, "bbox": page_box(local, offset),
                "source_segment_ids": list(slot.get("source_segment_ids", [])),
            })
        row_index += 1
    ranks = {column: i for i, column in enumerate(sorted(columns))}
    for cell in cells:
        cell["logical_column"] = ranks[cell["column"]]
    return cells, (max(1, len(rows)), max(1, maximum_column))


def block_metadata(stored, block, crop, compaction, cells, shape):
    """Existing packed u32 ABI v1, using the actual stage05 block and crop."""
    offset = tuple(stored.record["bbox"][:2])
    by_segment, spans = {}, {}
    for cell in cells:
        row, column = cell["physical_row"], cell["column"]
        for source_id in cell["source_segment_ids"]:
            by_segment.setdefault(source_id, []).append(cell)
            span = spans.setdefault(source_id, [row, row + 1, column, column + 1])
            span[:] = [min(span[0], row), max(span[1], row + 1),
                       min(span[2], column), max(span[3], column + 1)]
    placements = compaction.placements if compaction is not None else ()
    source_boxes = {segment.segment_id: page_box(segment.bbox, offset)
                    for segment in stored.segments}
    for placement in placements:
        for source_id in placement.segment_ids:
            source_boxes[source_id] = page_box(placement.source_bbox, offset)
    entries = []
    for fallback_row, source_id in enumerate(block.segment_ids):
        if stored.source_kind == "table" and by_segment.get(source_id):
            entries.extend((source_id, cell["bbox"],
                            [cell["physical_row"], cell["column"], 1, 1])
                           for cell in by_segment[source_id])
        else:
            span = spans.get(source_id, [fallback_row, fallback_row + 1, 0, 1])
            entries.append((source_id, source_boxes[source_id],
                            [span[0], span[2], span[1] - span[0], span[3] - span[2]]))
    metadata = [1, numeric_id(stored.source_object_id), KINDS.get(stored.source_kind, 3),
                *stored.record["bbox"], *page_box(block.bbox, offset),
                int(crop.bbox != block.bbox), int(block.matrix_window is not None),
                *(block.matrix_window or (0, 0, 0, 0)), *shape,
                len(block.core_segment_ids), *map(numeric_id, block.core_segment_ids),
                len(entries)]
    for source_id, bbox, cell in entries:
        metadata.extend([numeric_id(source_id), *bbox, *cell])
    metadata.extend([len(block.context_segment_ids),
                     *map(numeric_id, block.context_segment_ids), len(placements)])
    for placement in placements:
        members = set(placement.segment_ids)
        indexes = [i for i, entry in enumerate(entries) if entry[0] in members]
        metadata.extend([*page_box(placement.source_bbox, offset),
                         *placement.crop_bbox.as_tuple(), len(indexes), *indexes])
    if not entries or any(type(x) is not int or not 0 <= x <= 0xFFFFFFFF for x in metadata):
        raise ValueError(f"block cannot be represented by ABI v1: {block.block_id}")
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Frozen stage05 directory")
    parser.add_argument("--output", type=Path, required=True, help="New snapshot root; must not exist")
    parser.add_argument("--source-stage03", type=Path, required=True, help="Original stage03 provenance")
    parser.add_argument("--python-root", type=Path, default=DEFAULT_PYTHON_ROOT)
    parser.add_argument("--selection", type=Path, help="Policy selection JSON; defaults to sibling stage06")
    parser.add_argument("--object", action="append", dest="objects", help="Only this object directory name")
    parser.add_argument("--policy", action="append", default=[], metavar="OBJECT=POLICY")
    parser.add_argument("--trust-pickle", action="store_true", required=True,
                        help="Acknowledge trusted pickle; a digest is not an authenticity guarantee")
    args = parser.parse_args(argv)
    source = args.input.resolve(strict=True)
    stage03 = args.source_stage03.resolve(strict=True)
    output = args.output.resolve()
    if output.exists() or output == source.parent or source.parent in output.parents:
        raise ValueError("output must be new and outside the frozen item")
    python_root = args.python_root.resolve(strict=True)
    sys.path[:0] = [str(python_root / "scripts/debug"), str(python_root / "ocr")]
    import debug_object_stage as stage
    from PIL import Image

    if Path(stage.__file__).resolve() != python_root / "scripts/debug/debug_object_stage.py":
        raise ValueError("wrong debug_object_stage module already imported")
    selection = args.selection or source.parent / "06-get-segment/segment-topology-selection.json"
    selected = (json.loads(selection.read_text())["selected_policies"]
                if selection.is_file() else {})
    selected = dict(selected)
    for override in args.policy:
        name, policy = override.split("=", 1)
        selected[name] = policy
    object_dirs = sorted(path for path in (source / "objects").iterdir() if path.is_dir())
    if args.objects:
        requested = set(args.objects)
        object_dirs = [path for path in object_dirs if path.name in requested]
        if requested != {path.name for path in object_dirs}:
            raise ValueError("requested object is missing from stage05")
    states = []
    for object_dir in object_dirs:
        policy = selected.get(object_dir.name)
        if policy is None:
            candidates = sorted(object_dir.glob("*/state.pkl"))
            if len(candidates) != 1:
                raise ValueError(f"explicit policy required: {object_dir.name}")
            policy = candidates[0].parent.name
        if Path(policy).name != policy:
            raise ValueError("policy must be a directory name")
        state_dir = object_dir / policy
        stored, result = stage._read_state(state_dir)
        if not isinstance(stored, stage.recognition.StoredObject) or not isinstance(
                result, stage.recognition.BlockOcrResult):
            raise TypeError(f"not an OCR stage handoff: {state_dir}")
        if result.separated.policy != policy or stage._stored_stem(stored) != object_dir.name:
            raise ValueError(f"state identity mismatch: {state_dir}")
        states.append((state_dir, stored, result))
    if not states:
        raise ValueError("no selected OCR states")
    output.mkdir(parents=True, exist_ok=False)
    root = output / "05-ocr-blocks"
    (root / "blocks").mkdir(parents=True)

    def write(path, value):
        stage._write_json(path, value)

    records, jobs, layouts, objects, provenance = [], [], [], [], []
    for state_dir, stored, result in states:
        separated = result.separated
        object_id, policy = stored.source_object_id, separated.policy
        object_stem = stage._stored_stem(stored)
        target = root / "objects" / object_stem / policy
        target.mkdir(parents=True)
        cells, shape = matrix_cells(stored)
        layout = {"object_id": numeric_id(object_id), "source_object_id": object_id,
                  "object_kind": KINDS.get(stored.source_kind, 3), "source_kind": stored.source_kind,
                  "logical_row_count": shape[0] if stored.source_kind == "table" else 1,
                  "logical_column_count": shape[1] if stored.source_kind == "table" else 1,
                  "matrix_row_count": shape[0], "matrix_column_count": shape[1],
                  "layout_stage": "source-matrix-not-stage06"}
        layouts.append(layout)
        write(target / "object.json", stored.record)
        write(target / "matrix.json", stored.matrix_payload)
        write(target / "sparse-matrix.json", stored.matrix)
        write(target / "segments.json", stored.segments)
        write(target / "plan.json", separated.plan)
        write(target / "compactions.json", separated.compactions)
        write(target / "jobs.json", result.queue)
        (target / "object.png").write_bytes(stored.image_png)
        crops = {crop.block_id: crop for crop in separated.crops}
        compactions = {item.block_id: item for item in separated.compactions}
        block_indexes = {}
        for local_index, block in enumerate(separated.plan.blocks):
            key = (object_id, policy, block.block_id)
            if key in block_indexes:
                raise ValueError(f"duplicate block identity: {key}")
            block_indexes[key] = len(records)
            crop = crops[block.block_id]
            compaction = compactions.get(block.block_id)
            stem = f"block-{local_index + 1:06d}"
            raw_path = target / f"{stem}.raw.png"
            raw_path.write_bytes(crop.raw.png_bytes)
            if crop.gamma is not None:
                (target / f"{stem}.gamma.png").write_bytes(crop.gamma.png_bytes)
            if crop.isolation_mask_png is not None:
                (target / f"{stem}.mask.png").write_bytes(crop.isolation_mask_png)
            with Image.open(io.BytesIO(crop.raw.png_bytes)) as image:
                width, height = image.size
            payload = stage._block_payload(block, crop, index=local_index + 1, compaction=compaction)
            write(target / f"{stem}.json", payload)
            records.append({
                "metadata": block_metadata(stored, block, crop, compaction, cells, shape),
                "raster": "../" + str(raw_path.relative_to(root)),
                "width": width, "height": height, "stride": width * 3,
                "object_id": numeric_id(object_id), "source_object_id": object_id,
                "policy": policy, "block_id": block.block_id,
                "raw_sha256": hashlib.sha256(crop.raw.png_bytes).hexdigest(),
                "raster_role": "persisted-separated-raw-context-not-selected-attempt",
                "block_payload": payload,
            })
        for job in result.queue.jobs:
            if job.status.value != "complete" or job.output is None:
                raise ValueError(f"non-complete frozen job cannot be imported: {object_id}/{job.job_id}")
            jobs.append({
                "block_index": block_indexes[(object_id, policy, job.block_id)],
                "object_id": numeric_id(object_id), "source_object_id": object_id,
                "policy": policy, "block_id": job.block_id, "job_id": job.job_id,
                "languages": job.lane_id.replace("-", "+"), "lane_id": job.lane_id,
                "capability_id": job.capability_id,
                "transform": "gamma-dark" if job.transform.value == "gamma" else job.transform.value,
                "status": job.status.value, "input_sha256": job.input_sha256,
                "context_sha256": job.context_sha256, "text": job.output.text,
                "geometry": job.output.geometry.value, "grammar_milli": 0,
                # AdaptivePersistentOcrSession._map_full_output already restored
                # source-block-local coordinates before persisting queue.jobs.
                "words_in_source_space": True,
                "word_coordinate_space": "source-block-local",
                "confidence_units": "ppm-in-legacy-confidence_milli-field",
                "words": [{"text": word.text, "bbox": list(word.bbox.as_tuple()),
                           "confidence": word.confidence,
                           "confidence_milli": round(word.confidence * 1_000_000),
                           "word_index": index}
                          for index, word in enumerate(job.output.words)],
            })
        objects.append({"object_id": numeric_id(object_id), "source_object_id": object_id,
                        "policy": policy, "object_layout": layout, "matrix_cells": cells,
                        "membership_units": separated.plan.membership_units,
                        "diagnostics": separated.plan.diagnostics,
                        "artifacts": str(target.relative_to(root))})
        provenance.append({"state": str(state_dir / "state.pkl"), "digest_checked": True,
                           "source_stage03": str(stage03), "source_object_dir": str(stored.source_dir),
                           "object": object_stem, "policy": policy})
    extensions = ["objects.matrix_cells", "object_layouts", "policy",
                  "objects.membership_units", "jobs.job_id", "jobs.words.confidence"]
    write(root / "blocks/checkpoint.json", {
        "schema": "ittm.rust-selected-ocr-block-checkpoint/v2", "records": records,
        "object_layouts": layouts, "objects": objects, "required_parity_extensions": extensions,
    })
    write(root / "blocks/input.json", {"input": str(stage03), "provenance_only": True})
    write(root / "input.json", {"input": "blocks"})
    write(root / "checkpoint.json", {
        "schema": "ittm.rust-ocr-block-checkpoint/v2", "jobs": jobs,
        "object_layouts": layouts, "objects": objects, "required_parity_extensions": extensions,
    })
    summary = {"schema": "ittm.python-frozen-ocr-export/v2", "python_root": str(python_root),
               "input": str(source), "source_stage03": str(stage03), "state_provenance": provenance,
               "objects": len(objects), "blocks": len(records), "jobs": len(jobs),
               "matrix_cells": sum(len(obj["matrix_cells"]) for obj in objects),
               "empty_source_cells": sum(not cell["source_segment_ids"] for obj in objects
                                         for cell in obj["matrix_cells"]),
               "ocr_calls": 0, "stage_recomputations": 0,
               "note": "ABI v1 alone ignores parity extensions; this export does not claim stage06 parity."}
    write(output / "manifest.json", summary)
    print(json.dumps({"output": str(output), **{key: summary[key] for key in
          ("objects", "blocks", "jobs", "matrix_cells", "empty_source_cells", "ocr_calls")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
