#!/usr/bin/env python3
"""Independent pixel-truth audit for Stage 1 geometry artifacts.

This auditor intentionally does not import ``app.sparse_pipeline``.  It reads
the published Stage 1 files, reconstructs ownership from terminal recursive
leaves plus the foreground/rule masks, labels components with OpenCV, and then
rebuilds the sparse matrix from pixels.  Existing contract constructors and
unit-test assertions therefore cannot make this audit pass by agreeing with
themselves.

The command writes a new immutable run directory.  An automated clean result
is still ``MANUAL_REVIEW_PENDING``: visual review of every matrix page and the
selected actual crops is a separate, explicit gate.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw


_CONTACT_RANGE = re.compile(r"^segments-(\d{6})-(\d{6})\.png$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class AuditCheck:
    check_id: str
    status: str
    severity: str
    count: int
    evidence: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_pixels(value: np.ndarray) -> str:
    return hashlib.sha256(memoryview(np.ascontiguousarray(value))).hexdigest()


def _load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB"), dtype=np.uint8)


def _load_mask(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("L"), dtype=np.uint8) != 0


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            value = json.loads(stripped)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSON object expected")
            values.append(value)
    return values


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[object]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def _box(value: object) -> tuple[int, int, int, int]:
    if isinstance(value, dict):
        return (
            int(value["left"]),
            int(value["top"]),
            int(value["right"]),
            int(value["bottom"]),
        )
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return tuple(int(item) for item in value)  # type: ignore[return-value]
    raise ValueError(f"invalid box: {value!r}")


def _intersects(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bool:
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


def _true_runs(values: np.ndarray) -> tuple[tuple[int, int], ...]:
    indexes = np.flatnonzero(values)
    if indexes.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(indexes) > 1)
    starts = np.concatenate((indexes[:1], indexes[breaks + 1]))
    stops = np.concatenate((indexes[breaks] + 1, indexes[-1:] + 1))
    return tuple((int(start), int(stop)) for start, stop in zip(starts, stops))


def _axis_lookup(
    intervals: Sequence[dict[str, object]], length: int
) -> tuple[np.ndarray, list[str]]:
    errors: list[str] = []
    lookup = np.full(length, -1, dtype=np.int32)
    expected_start = 0
    for expected_index, interval in enumerate(intervals):
        index = int(interval["index"])
        start = int(interval["start"])
        end = int(interval["end"])
        if index != expected_index:
            errors.append(f"index {index} != {expected_index}")
        if start != expected_start:
            errors.append(f"interval {index} starts {start}, expected {expected_start}")
        if not 0 <= start < end <= length:
            errors.append(f"interval {index} outside 0:{length}: {start}:{end}")
            continue
        if np.any(lookup[start:end] != -1):
            errors.append(f"interval {index} overlaps a previous interval")
        lookup[start:end] = index
        expected_start = end
    if expected_start != length:
        errors.append(f"axis ends {expected_start}, expected {length}")
    if np.any(lookup < 0):
        errors.append(f"axis leaves {int(np.count_nonzero(lookup < 0))} coordinates uncovered")
    return lookup, errors


def _expected_children(
    node: dict[str, object], children: Sequence[dict[str, object]]
) -> tuple[tuple[int, int, int, int], ...] | None:
    parent = _box(node["bbox"])
    axis = node.get("axis")
    split = node.get("split_coordinate")
    separators = node.get("separator_boxes") or []
    if len(children) != 2:
        return None
    if split is not None:
        coordinate = int(split)
        if axis == "rows":
            return (
                (parent[0], parent[1], parent[2], coordinate),
                (parent[0], coordinate, parent[2], parent[3]),
            )
        if axis == "columns":
            return (
                (parent[0], parent[1], coordinate, parent[3]),
                (coordinate, parent[1], parent[2], parent[3]),
            )
        return None
    if len(separators) != 1:
        return None
    separator = _box(separators[0])
    if axis == "rows":
        return (
            (parent[0], parent[1], parent[2], separator[1]),
            (parent[0], separator[3], parent[2], parent[3]),
        )
    if axis == "columns":
        return (
            (parent[0], parent[1], separator[0], parent[3]),
            (separator[2], parent[1], parent[2], parent[3]),
        )
    return None


def _add_check(
    checks: list[AuditCheck],
    check_id: str,
    *,
    failures: Sequence[object] | int,
    severity: str,
    evidence: str,
) -> None:
    count = failures if isinstance(failures, int) else len(failures)
    checks.append(
        AuditCheck(
            check_id=check_id,
            status="PASS" if count == 0 else "FAIL",
            severity=severity,
            count=int(count),
            evidence=evidence,
        )
    )


def _render_pair_sheet(
    output: Path,
    *,
    items: Sequence[tuple[str, Path, Path]],
    start: int,
    stop: int,
) -> None:
    tile_width, tile_height = 420, 230
    columns = 3
    subset = items[start:stop]
    rows = max(1, math.ceil(len(subset) / columns))
    sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        for offset, (label, raw_path, isolated_path) in enumerate(subset):
            column = offset % columns
            row = offset // columns
            left = column * tile_width
            top = row * tile_height
            draw.rectangle(
                (left, top, left + tile_width - 1, top + tile_height - 1),
                outline=(175, 175, 175),
            )
            draw.text((left + 6, top + 5), label[:62], fill=(0, 0, 0))
            for side, path in enumerate((raw_path, isolated_path)):
                with Image.open(path) as opened:
                    preview = opened.convert("RGB")
                try:
                    preview.thumbnail(
                        (tile_width // 2 - 16, tile_height - 42),
                        Image.Resampling.NEAREST,
                    )
                    paste_left = (
                        left
                        + side * (tile_width // 2)
                        + (tile_width // 2 - preview.width) // 2
                    )
                    paste_top = top + 31 + (tile_height - 35 - preview.height) // 2
                    sheet.paste(preview, (paste_left, paste_top))
                finally:
                    preview.close()
        sheet.save(output, format="PNG")
    finally:
        sheet.close()


def _target_definitions(fixture_name: str) -> tuple[dict[str, object], ...]:
    if fixture_name != "09.03.03_05(ИУ1).pdf.raster.png":
        return ()
    return (
        {
            "target_id": "canonical-old361-region",
            "bbox": [172, 1055, 3552, 1109],
            "note": "Rejected-v4 region label; final IDs are intentionally ignored.",
        },
        {
            "target_id": "reported-merged-region",
            "bbox": [172, 1073, 1524, 1296],
            "note": "Reject only if an independent separable cut is proven.",
        },
        {
            "target_id": "weak-horizontal-component",
            "bbox": [821, 1105, 891, 1109],
            "note": "Do not infer rule semantics; record pixel ownership only.",
        },
        {
            "target_id": "canonical-old758-region",
            "bbox": [172, 2734, 3552, 2772],
            "note": "Rejected-v4 region label; final IDs are intentionally ignored.",
        },
    )


def _self_test() -> int:
    root = {
        "node_id": "root",
        "bbox": {"left": 0, "top": 0, "right": 12, "bottom": 10},
        "axis": "rows",
        "split_coordinate": 5,
        "separator_boxes": [],
    }
    children = (
        {"bbox": {"left": 0, "top": 0, "right": 12, "bottom": 5}},
        {"bbox": {"left": 0, "top": 5, "right": 12, "bottom": 10}},
    )
    assert _expected_children(root, children) == (
        (0, 0, 12, 5),
        (0, 5, 12, 10),
    )
    broken = (
        children[0],
        {"bbox": {"left": 0, "top": 6, "right": 12, "bottom": 10}},
    )
    assert tuple(_box(item["bbox"]) for item in broken) != _expected_children(
        root, broken
    )
    values = np.asarray([False, True, True, False, True, False])
    assert _true_runs(values) == ((1, 3), (4, 5))
    intervals = [
        {"index": 0, "start": 0, "end": 5},
        {"index": 1, "start": 5, "end": 10},
    ]
    lookup, errors = _axis_lookup(intervals, 10)
    assert not errors and lookup.tolist() == [0] * 5 + [1] * 5
    _, errors = _axis_lookup(intervals[:1], 10)
    assert errors

    # Independent OpenCV connectivity must expose a component cut by a leaf
    # boundary even when aggregate foreground counts still match.
    mask = np.zeros((10, 12), dtype=np.uint8)
    mask[4:7, 4:8] = 1
    count, labels, _, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8, ltype=cv2.CV_32S
    )
    assert count == 2
    leaf_map = np.zeros_like(labels, dtype=np.int32)
    leaf_map[5:] = 1
    pairs = np.unique(labels[mask != 0].astype(np.int64) * 3 + leaf_map[mask != 0])
    assert len(pairs) == 2, "one component must be observed in both leaves"
    print("synthetic self-test: PASS")
    return 0


def audit(
    *,
    fixture: Path,
    geometry_run: Path,
    output_root: Path,
    run_id: str,
) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("unsafe run id")
    stage = geometry_run / "01-geometry" if (geometry_run / "01-geometry").is_dir() else geometry_run
    required = (
        "manifest.json",
        "alignment.json",
        "nodes.jsonl",
        "segments.jsonl",
        "rules.jsonl",
        "matrix.json",
        "source.png",
        "aligned.png",
        "foreground-mask.png",
        "rule-mask.png",
    )
    missing = [name for name in required if not (stage / name).is_file()]
    if missing:
        raise FileNotFoundError(f"geometry artifacts missing: {missing}")
    if not fixture.is_file():
        raise FileNotFoundError(fixture)
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(f"immutable audit run already exists: {final}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        checks: list[AuditCheck] = []
        findings: list[dict[str, object]] = []
        source_fixture = _load_rgb(fixture)
        source = _load_rgb(stage / "source.png")
        aligned = _load_rgb(stage / "aligned.png")
        foreground = _load_mask(stage / "foreground-mask.png")
        rule_mask = _load_mask(stage / "rule-mask.png")
        height, width = foreground.shape
        manifest = _load_json(stage / "manifest.json")
        alignment = _load_json(stage / "alignment.json")
        matrix = _load_json(stage / "matrix.json")
        nodes = _load_jsonl(stage / "nodes.jsonl")
        segments = _load_jsonl(stage / "segments.jsonl")
        rules = _load_jsonl(stage / "rules.jsonl")
        if not isinstance(manifest, dict) or not isinstance(alignment, dict) or not isinstance(matrix, dict):
            raise ValueError("top-level geometry JSON values must be objects")

        _add_check(
            checks,
            "source-pixels-equal-fixture",
            failures=0 if np.array_equal(source, source_fixture) else 1,
            severity="critical",
            evidence="checks.jsonl; source.png; fixture",
        )
        shape_errors = []
        for name, value in (
            ("aligned", aligned),
            ("foreground", foreground),
            ("rule", rule_mask),
        ):
            if value.shape[:2] != (height, width):
                shape_errors.append(f"{name}:{value.shape}")
        if source.shape != source_fixture.shape:
            shape_errors.append(
                f"source:{source.shape} != fixture:{source_fixture.shape}"
            )
        _add_check(
            checks,
            "artifact-shapes-agree",
            failures=shape_errors,
            severity="critical",
            evidence="checks.jsonl",
        )
        hash_failures = []
        if manifest.get("source_sha256") != _sha256_pixels(source):
            hash_failures.append("source pixel SHA")
        if manifest.get("aligned_rgb_sha256") != _sha256_pixels(aligned):
            hash_failures.append("aligned pixel SHA")
        _add_check(
            checks,
            "published-pixel-sha",
            failures=hash_failures,
            severity="critical",
            evidence="manifest.json versus decoded pixels",
        )

        outside_rule = np.logical_and(rule_mask, np.logical_not(foreground))
        _add_check(
            checks,
            "rule-mask-subset-foreground",
            failures=int(np.count_nonzero(outside_rule)),
            severity="critical",
            evidence="pixel-findings.jsonl",
        )
        non_rule = np.logical_and(foreground, np.logical_not(rule_mask))

        node_by_id = {str(node["node_id"]): node for node in nodes}
        duplicate_node_ids = len(nodes) - len(node_by_id)
        tree_failures: list[dict[str, object]] = []
        separator_failures: list[dict[str, object]] = []
        root_nodes = [node for node in nodes if node.get("parent_id") is None]
        if len(root_nodes) != 1 or (root_nodes and _box(root_nodes[0]["bbox"]) != (0, 0, width, height)):
            tree_failures.append({"kind": "root", "root_count": len(root_nodes)})
        for node in nodes:
            node_id = str(node["node_id"])
            child_ids = tuple(str(value) for value in node.get("child_ids") or [])
            if child_ids:
                children = [node_by_id.get(child_id) for child_id in child_ids]
                if any(child is None for child in children):
                    tree_failures.append({"node_id": node_id, "kind": "missing-child"})
                    continue
                expected = _expected_children(node, children)  # type: ignore[arg-type]
                actual = tuple(_box(child["bbox"]) for child in children if child is not None)
                if expected is None or actual != expected:
                    tree_failures.append(
                        {
                            "node_id": node_id,
                            "kind": "children-do-not-exactly-partition-parent",
                            "expected": expected,
                            "actual": actual,
                        }
                    )
                for child in children:
                    assert child is not None
                    if child.get("parent_id") != node_id or int(child.get("depth", -1)) != int(node.get("depth", -2)) + 1:
                        tree_failures.append(
                            {"node_id": node_id, "child_id": child.get("node_id"), "kind": "parent-depth"}
                        )
                for raw_separator in node.get("separator_boxes") or []:
                    left, top, right, bottom = _box(raw_separator)
                    segment_pixels = int(np.count_nonzero(non_rule[top:bottom, left:right]))
                    if segment_pixels:
                        separator_failures.append(
                            {
                                "node_id": node_id,
                                "bbox": [left, top, right, bottom],
                                "non_rule_foreground_pixels": segment_pixels,
                            }
                        )
            else:
                if node.get("axis") is not None or node.get("stop_reason") not in {"atomic", "empty", "limit"}:
                    tree_failures.append({"node_id": node_id, "kind": "invalid-terminal-trace"})
        _write_jsonl(temporary / "tree-failures.jsonl", tree_failures)
        _write_jsonl(temporary / "separator-pixel-failures.jsonl", separator_failures)
        _add_check(
            checks,
            "recursive-tree-exact-half-open-partition",
            failures=duplicate_node_ids + len(tree_failures),
            severity="critical",
            evidence="tree.tsv; tree-failures.jsonl",
        )
        _add_check(
            checks,
            "recursive-separators-do-not-consume-segment-pixels",
            failures=separator_failures,
            severity="critical",
            evidence="separator-pixel-failures.jsonl",
        )

        leaves = [node for node in nodes if not (node.get("child_ids") or [])]
        leaf_raster = np.full((height, width), -1, dtype=np.int32)
        leaf_overlap_pixels = 0
        for leaf_index, leaf in enumerate(leaves):
            left, top, right, bottom = _box(leaf["bbox"])
            current = leaf_raster[top:bottom, left:right]
            leaf_overlap_pixels += int(np.count_nonzero(current >= 0))
            current[current < 0] = leaf_index
        missing_leaf_pixels = np.logical_and(non_rule, leaf_raster < 0)
        _add_check(
            checks,
            "non-rule-foreground-has-one-terminal-leaf",
            failures=leaf_overlap_pixels + int(np.count_nonzero(missing_leaf_pixels)),
            severity="critical",
            evidence="pixel-findings.jsonl",
        )

        segment_ids = [str(segment["segment_id"]) for segment in segments]
        segment_index = {segment_id: index for index, segment_id in enumerate(segment_ids)}
        duplicate_segment_ids = len(segment_ids) - len(segment_index)
        leaf_to_segment = np.full(len(leaves), -1, dtype=np.int32)
        segment_to_leaf: dict[str, int] = {}
        leaf_segment_failures: list[dict[str, object]] = []
        for leaf_index, leaf in enumerate(leaves):
            values = tuple(str(value) for value in leaf.get("segment_ids") or [])
            if len(values) > 1:
                leaf_segment_failures.append(
                    {"node_id": leaf["node_id"], "segment_ids": values, "kind": "multiple-segments"}
                )
                continue
            if values:
                segment_id = values[0]
                if segment_id not in segment_index:
                    leaf_segment_failures.append(
                        {"node_id": leaf["node_id"], "segment_id": segment_id, "kind": "unknown-segment"}
                    )
                    continue
                if segment_id in segment_to_leaf:
                    leaf_segment_failures.append(
                        {"node_id": leaf["node_id"], "segment_id": segment_id, "kind": "duplicate-leaf-owner"}
                    )
                    continue
                leaf_to_segment[leaf_index] = segment_index[segment_id]
                segment_to_leaf[segment_id] = leaf_index
        for segment_id in segment_ids:
            if segment_id not in segment_to_leaf:
                leaf_segment_failures.append({"segment_id": segment_id, "kind": "no-leaf-owner"})
        _write_jsonl(temporary / "leaf-segment-failures.jsonl", leaf_segment_failures)
        _add_check(
            checks,
            "terminal-leaf-segment-bijection",
            failures=duplicate_segment_ids + len(leaf_segment_failures),
            severity="critical",
            evidence="leaf-segment-failures.jsonl",
        )

        orphan_non_rule_pixels = 0
        valid_pixels = np.logical_and(non_rule, leaf_raster >= 0)
        if np.any(valid_pixels):
            mapped = leaf_to_segment[leaf_raster[valid_pixels]]
            orphan_non_rule_pixels = int(np.count_nonzero(mapped < 0))
        _add_check(
            checks,
            "every-non-rule-pixel-maps-to-segment",
            failures=orphan_non_rule_pixels,
            severity="critical",
            evidence="pixel-findings.jsonl",
        )

        # Independent 8-connected component labelling.  A non-rule component
        # split across terminal leaves proves that a cut crossed owned ink.
        component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(
            non_rule.astype(np.uint8), connectivity=8, ltype=cv2.CV_32S
        )
        foreground_labels = component_labels[non_rule]
        foreground_leaves = leaf_raster[non_rule]
        pair_base = len(leaves) + 1
        pair_codes = np.unique(
            foreground_labels.astype(np.int64) * pair_base
            + (foreground_leaves.astype(np.int64) + 1)
        )
        component_to_leaves: dict[int, set[int]] = defaultdict(set)
        for code in pair_codes:
            component_label = int(code // pair_base)
            leaf_index = int(code % pair_base) - 1
            component_to_leaves[component_label].add(leaf_index)
        cut_components: list[dict[str, object]] = []
        for label, owner_leaves in component_to_leaves.items():
            if len(owner_leaves) == 1 and next(iter(owner_leaves)) >= 0:
                continue
            x, y, component_width, component_height, pixels = (
                int(value) for value in component_stats[label]
            )
            cut_components.append(
                {
                    "component_label": label,
                    "bbox": [x, y, x + component_width, y + component_height],
                    "pixels": pixels,
                    "leaf_indices": sorted(owner_leaves),
                    "leaf_ids": [
                        leaves[index]["node_id"] if index >= 0 else "<no-leaf>"
                        for index in sorted(owner_leaves)
                    ],
                }
            )
        _write_jsonl(temporary / "cut-components.jsonl", cut_components)
        checks.append(
            AuditCheck(
                check_id="coordinate-cuts-crossing-connected-foreground-review",
                status="REVIEW" if cut_components else "PASS",
                severity="review",
                count=len(cut_components),
                evidence="cut-components.jsonl; suspicious-crop-sheets/",
            )
        )

        # Reconstruct exact segment pixels from leaf rectangles, not from the
        # production ownership raster or the crop writer.
        segment_pixel_failures: list[dict[str, object]] = []
        segment_pixel_counts = np.zeros(len(segments), dtype=np.int64)
        for segment_id, leaf_index in segment_to_leaf.items():
            index = segment_index[segment_id]
            leaf = leaves[leaf_index]
            left, top, right, bottom = _box(leaf["bbox"])
            local = non_rule[top:bottom, left:right]
            rows, columns = np.nonzero(local)
            if rows.size == 0:
                segment_pixel_failures.append({"segment_id": segment_id, "kind": "zero-independent-pixels"})
                continue
            actual_bbox = [
                left + int(columns.min()),
                top + int(rows.min()),
                left + int(columns.max()) + 1,
                top + int(rows.max()) + 1,
            ]
            declared = segments[index]
            declared_bbox = list(_box(declared["bbox"]))
            pixels = int(rows.size)
            segment_pixel_counts[index] = pixels
            if declared_bbox != actual_bbox or int(declared["ink_pixels"]) != pixels:
                segment_pixel_failures.append(
                    {
                        "segment_id": segment_id,
                        "declared_bbox": declared_bbox,
                        "independent_bbox": actual_bbox,
                        "declared_pixels": int(declared["ink_pixels"]),
                        "independent_pixels": pixels,
                    }
                )
        _write_jsonl(temporary / "segment-pixel-failures.jsonl", segment_pixel_failures)
        _add_check(
            checks,
            "segment-bbox-and-count-equal-independent-pixels",
            failures=segment_pixel_failures,
            severity="critical",
            evidence="segment-pixel-failures.jsonl",
        )

        declared_segment_pixels = sum(int(segment["ink_pixels"]) for segment in segments)
        declared_rule_pixels = sum(int(rule["foreground_pixels"]) for rule in rules)
        ownership_equation_failures = []
        if declared_segment_pixels != int(np.count_nonzero(non_rule)):
            ownership_equation_failures.append("segment total")
        if declared_rule_pixels != int(np.count_nonzero(rule_mask)):
            ownership_equation_failures.append("rule total")
        if declared_segment_pixels + declared_rule_pixels != int(np.count_nonzero(foreground)):
            ownership_equation_failures.append("foreground total")
        _add_check(
            checks,
            "foreground-owned-exactly-once",
            failures=ownership_equation_failures,
            severity="critical",
            evidence="checks.jsonl",
        )

        # Rule bbox union must cover every rule-owned pixel.  We do not infer
        # rule semantics from aspect ratio and do not treat an underline as a
        # separator without further geometric evidence.
        rule_cover = np.zeros_like(rule_mask)
        rule_bbox_failures: list[dict[str, object]] = []
        for rule in rules:
            left, top, right, bottom = _box(rule["bbox"])
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                rule_bbox_failures.append({"rule_id": rule["rule_id"], "kind": "outside-canvas"})
                continue
            rule_cover[top:bottom, left:right] = True
            if not np.any(rule_mask[top:bottom, left:right]):
                rule_bbox_failures.append({"rule_id": rule["rule_id"], "kind": "zero-rule-mask-pixels"})
        uncovered_rule_pixels = int(np.count_nonzero(np.logical_and(rule_mask, np.logical_not(rule_cover))))
        _write_jsonl(temporary / "rule-bbox-failures.jsonl", rule_bbox_failures)
        _add_check(
            checks,
            "rule-pixels-covered-by-declared-rule-box",
            failures=uncovered_rule_pixels + len(rule_bbox_failures),
            severity="critical",
            evidence="rule-bbox-failures.jsonl",
        )
        del rule_cover

        rows = matrix.get("rows") or []
        columns = matrix.get("columns") or []
        cells = matrix.get("cells") or []
        spans = matrix.get("spans") or []
        row_lookup, row_axis_errors = _axis_lookup(rows, height)
        column_lookup, column_axis_errors = _axis_lookup(columns, width)
        _write_json(temporary / "axis-errors.json", {"rows": row_axis_errors, "columns": column_axis_errors})
        _add_check(
            checks,
            "matrix-axes-cover-aligned-canvas",
            failures=len(row_axis_errors) + len(column_axis_errors),
            severity="critical",
            evidence="axis-errors.json; grid-boundaries.tsv",
        )

        # Matrix boundaries must be exactly the union of rule edges, recursive
        # separator edges, and component split coordinates.
        expected_row_cuts = {0, height}
        expected_column_cuts = {0, width}
        cut_sources: dict[tuple[str, int], set[str]] = defaultdict(set)
        for coordinate, label in ((0, "canvas"), (height, "canvas")):
            cut_sources[("row", coordinate)].add(label)
        for coordinate, label in ((0, "canvas"), (width, "canvas")):
            cut_sources[("column", coordinate)].add(label)
        for rule in rules:
            left, top, right, bottom = _box(rule["bbox"])
            expected_row_cuts.update((top, bottom))
            expected_column_cuts.update((left, right))
            cut_sources[("row", top)].add(f"rule:{rule['rule_id']}:top")
            cut_sources[("row", bottom)].add(f"rule:{rule['rule_id']}:bottom")
            cut_sources[("column", left)].add(f"rule:{rule['rule_id']}:left")
            cut_sources[("column", right)].add(f"rule:{rule['rule_id']}:right")
        for node in nodes:
            node_id = str(node["node_id"])
            split = node.get("split_coordinate")
            if split is not None:
                coordinate = int(split)
                if node.get("axis") == "rows":
                    expected_row_cuts.add(coordinate)
                    cut_sources[("row", coordinate)].add(f"node:{node_id}:split")
                else:
                    expected_column_cuts.add(coordinate)
                    cut_sources[("column", coordinate)].add(f"node:{node_id}:split")
            for separator_value in node.get("separator_boxes") or []:
                left, top, right, bottom = _box(separator_value)
                expected_row_cuts.update((top, bottom))
                expected_column_cuts.update((left, right))
                cut_sources[("row", top)].add(f"node:{node_id}:separator-top")
                cut_sources[("row", bottom)].add(f"node:{node_id}:separator-bottom")
                cut_sources[("column", left)].add(f"node:{node_id}:separator-left")
                cut_sources[("column", right)].add(f"node:{node_id}:separator-right")
        actual_row_cuts = {int(interval["start"]) for interval in rows}
        actual_column_cuts = {int(interval["start"]) for interval in columns}
        if rows:
            actual_row_cuts.add(int(rows[-1]["end"]))
        if columns:
            actual_column_cuts.add(int(columns[-1]["end"]))
        boundary_diff = {
            "missing_rows": sorted(expected_row_cuts - actual_row_cuts),
            "extra_rows": sorted(actual_row_cuts - expected_row_cuts),
            "missing_columns": sorted(expected_column_cuts - actual_column_cuts),
            "extra_columns": sorted(actual_column_cuts - expected_column_cuts),
        }
        _write_json(temporary / "boundary-diff.json", boundary_diff)
        _add_check(
            checks,
            "matrix-cuts-equal-recursion-and-rule-evidence",
            failures=sum(len(value) for value in boundary_diff.values()),
            severity="critical",
            evidence="boundary-diff.json; grid-boundaries.tsv",
        )

        with (temporary / "grid-boundaries.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("axis", "index", "start", "end", "start_sources", "end_sources"))
            for axis, intervals in (("row", rows), ("column", columns)):
                for interval in intervals:
                    start = int(interval["start"])
                    end = int(interval["end"])
                    writer.writerow(
                        (
                            axis,
                            interval["index"],
                            start,
                            end,
                            ";".join(sorted(cut_sources[(axis, start)])),
                            ";".join(sorted(cut_sources[(axis, end)])),
                        )
                    )

        # Exact sparse cell reconstruction from independently owned pixels.
        pixel_counts_by_code: dict[int, int] = defaultdict(int)
        segment_total = len(segments)
        column_total = len(columns)
        for pixel_row in range(height):
            pixel_columns = np.flatnonzero(non_rule[pixel_row])
            if pixel_columns.size == 0:
                continue
            leaf_indexes = leaf_raster[pixel_row, pixel_columns]
            valid = leaf_indexes >= 0
            if not np.any(valid):
                continue
            pixel_columns = pixel_columns[valid]
            owners = leaf_to_segment[leaf_indexes[valid]]
            valid_owner = owners >= 0
            pixel_columns = pixel_columns[valid_owner]
            owners = owners[valid_owner]
            codes = (
                (
                    int(row_lookup[pixel_row]) * column_total
                    + column_lookup[pixel_columns].astype(np.int64)
                )
                * segment_total
                + owners.astype(np.int64)
            )
            unique_codes, counts = np.unique(codes, return_counts=True)
            for code, count in zip(unique_codes, counts):
                pixel_counts_by_code[int(code)] += int(count)

        expected_cells: dict[tuple[int, int, str], int] = {}
        for code, count in pixel_counts_by_code.items():
            owner = code % segment_total
            matrix_coordinate = code // segment_total
            column = matrix_coordinate % column_total
            row = matrix_coordinate // column_total
            expected_cells[(row, column, segment_ids[owner])] = count
        declared_cell_keys = [
            (int(cell["row"]), int(cell["column"]), str(cell["segment_id"]))
            for cell in cells
        ]
        declared_cell_set = set(declared_cell_keys)
        expected_cell_set = set(expected_cells)
        cell_diff = {
            "duplicate_declared_entries": len(declared_cell_keys) - len(declared_cell_set),
            "missing_declared_cells": [list(value) for value in sorted(expected_cell_set - declared_cell_set)],
            "extra_zero_pixel_cells": [list(value) for value in sorted(declared_cell_set - expected_cell_set)],
        }
        _write_json(temporary / "matrix-cell-diff.json", cell_diff)
        _add_check(
            checks,
            "matrix-cells-equal-pixel-backed-cells",
            failures=(
                int(cell_diff["duplicate_declared_entries"])
                + len(cell_diff["missing_declared_cells"])
                + len(cell_diff["extra_zero_pixel_cells"])
            ),
            severity="critical",
            evidence="matrix-cell-diff.json; matrix-cells.jsonl",
        )

        declared_span_by_segment = {str(span["segment_id"]): span for span in spans}
        span_failures: list[dict[str, object]] = []
        expected_by_segment: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for row, column, segment_id in expected_cell_set:
            expected_by_segment[segment_id].append((row, column))
        for segment_id in segment_ids:
            values = expected_by_segment.get(segment_id, [])
            span = declared_span_by_segment.get(segment_id)
            if not values or span is None:
                span_failures.append({"segment_id": segment_id, "kind": "missing-pixels-or-span"})
                continue
            expected_span = [
                min(value[0] for value in values),
                max(value[0] for value in values) + 1,
                min(value[1] for value in values),
                max(value[1] for value in values) + 1,
            ]
            actual_span = [
                int(span["row_start"]),
                int(span["row_stop"]),
                int(span["column_start"]),
                int(span["column_stop"]),
            ]
            if expected_span != actual_span:
                span_failures.append(
                    {"segment_id": segment_id, "expected": expected_span, "declared": actual_span}
                )
        unknown_spans = set(declared_span_by_segment) - set(segment_ids)
        span_failures.extend({"segment_id": value, "kind": "unknown-span"} for value in sorted(unknown_spans))
        _write_jsonl(temporary / "matrix-span-failures.jsonl", span_failures)
        _add_check(
            checks,
            "matrix-spans-equal-pixel-backed-cell-extents",
            failures=span_failures,
            severity="critical",
            evidence="matrix-span-failures.jsonl",
        )

        expected_horizontal_rows = sorted(
            {
                int(interval["index"])
                for rule in rules
                if rule.get("axis") == "horizontal"
                for interval in rows
                if int(interval["start"]) < _box(rule["bbox"])[3]
                and _box(rule["bbox"])[1] < int(interval["end"])
            }
        )
        expected_vertical_columns = sorted(
            {
                int(interval["index"])
                for rule in rules
                if rule.get("axis") == "vertical"
                for interval in columns
                if int(interval["start"]) < _box(rule["bbox"])[2]
                and _box(rule["bbox"])[0] < int(interval["end"])
            }
        )
        rule_axis_diff = {
            "expected_horizontal_rows": expected_horizontal_rows,
            "declared_horizontal_rows": matrix.get("horizontal_rule_rows") or [],
            "expected_vertical_columns": expected_vertical_columns,
            "declared_vertical_columns": matrix.get("vertical_rule_columns") or [],
        }
        _write_json(temporary / "matrix-rule-axis-diff.json", rule_axis_diff)
        _add_check(
            checks,
            "matrix-rule-axis-indexes-equal-rule-pixels",
            failures=(
                0
                if expected_horizontal_rows == (matrix.get("horizontal_rule_rows") or [])
                and expected_vertical_columns == (matrix.get("vertical_rule_columns") or [])
                else 1
            ),
            severity="critical",
            evidence="matrix-rule-axis-diff.json",
        )

        _write_jsonl(
            temporary / "matrix-cells.jsonl",
            (
                {
                    "row": row,
                    "row_interval": [int(rows[row]["start"]), int(rows[row]["end"])],
                    "column": column,
                    "column_interval": [
                        int(columns[column]["start"]),
                        int(columns[column]["end"]),
                    ],
                    "segment_id": segment_id,
                    "owned_pixels": expected_cells[(row, column, segment_id)],
                }
                for row, column, segment_id in sorted(expected_cells)
            ),
        )

        # Full tree ledger, including every split trace and terminal reason.
        with (temporary / "tree.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(
                (
                    "node_id",
                    "parent_id",
                    "depth",
                    "bbox",
                    "axis",
                    "split_coordinate",
                    "separator_boxes",
                    "child_ids",
                    "terminal_reason",
                    "segment_ids",
                )
            )
            for node in nodes:
                writer.writerow(
                    (
                        node["node_id"],
                        node.get("parent_id") or "",
                        node["depth"],
                        ",".join(str(value) for value in _box(node["bbox"])),
                        node.get("axis") or "",
                        node.get("split_coordinate") if node.get("split_coordinate") is not None else "",
                        json.dumps(node.get("separator_boxes") or [], separators=(",", ":")),
                        ",".join(str(value) for value in node.get("child_ids") or []),
                        node.get("stop_reason") or "",
                        ",".join(str(value) for value in node.get("segment_ids") or []),
                    )
                )

        # Geometric missed-cut candidates are review evidence, never semantic
        # verdicts.  Large bboxes alone are deliberately not failures.
        raw_gap_candidates: list[dict[str, object]] = []
        leaf_profiles: list[dict[str, object]] = []
        for leaf_index, leaf in enumerate(leaves):
            left, top, right, bottom = _box(leaf["bbox"])
            local = non_rule[top:bottom, left:right]
            pixels = int(np.count_nonzero(local))
            horizontal_runs: list[dict[str, object]] = []
            vertical_runs: list[dict[str, object]] = []
            if pixels:
                row_projection = local.sum(axis=1)
                column_projection = local.sum(axis=0)
                for start, stop in _true_runs(row_projection == 0):
                    if start == 0 or stop == local.shape[0]:
                        continue
                    upper = local[:start]
                    lower = local[stop:]
                    upper_pixels = int(np.count_nonzero(upper))
                    lower_pixels = int(np.count_nonzero(lower))
                    if not upper_pixels or not lower_pixels:
                        continue
                    upper_width = int(np.count_nonzero(upper.any(axis=0)))
                    lower_width = int(np.count_nonzero(lower.any(axis=0)))
                    balance = min(upper_pixels, lower_pixels) / max(upper_pixels, lower_pixels)
                    candidate = {
                        "axis": "rows",
                        "band": [top + start, top + stop],
                        "gap": stop - start,
                        "pixel_balance": balance,
                        "upper_width": upper_width,
                        "lower_width": lower_width,
                    }
                    horizontal_runs.append(candidate)
                    if balance >= 0.12 and min(upper_width, lower_width) >= max(1, round((right - left) * 0.15)):
                        raw_gap_candidates.append(
                            {
                                "leaf_id": leaf["node_id"],
                                "leaf_bbox": [left, top, right, bottom],
                                **candidate,
                                "classification": "manual-geometric-review",
                            }
                        )
                for start, stop in _true_runs(column_projection == 0):
                    if start == 0 or stop == local.shape[1] or stop - start < 4:
                        continue
                    before_pixels = int(np.count_nonzero(local[:, :start]))
                    after_pixels = int(np.count_nonzero(local[:, stop:]))
                    if not before_pixels or not after_pixels:
                        continue
                    balance = min(before_pixels, after_pixels) / max(before_pixels, after_pixels)
                    candidate = {
                        "axis": "columns",
                        "band": [left + start, left + stop],
                        "gap": stop - start,
                        "pixel_balance": balance,
                    }
                    vertical_runs.append(candidate)
                    if balance >= 0.12:
                        raw_gap_candidates.append(
                            {
                                "leaf_id": leaf["node_id"],
                                "leaf_bbox": [left, top, right, bottom],
                                **candidate,
                                "classification": "manual-geometric-review-only",
                            }
                        )
            leaf_profiles.append(
                {
                    "leaf_id": leaf["node_id"],
                    "bbox": [left, top, right, bottom],
                    "area": (right - left) * (bottom - top),
                    "non_rule_pixels": pixels,
                    "terminal_reason": leaf.get("stop_reason"),
                    "segment_ids": leaf.get("segment_ids") or [],
                    "horizontal_blank_bands": horizontal_runs,
                    "vertical_blank_bands": vertical_runs,
                }
            )
        _write_jsonl(temporary / "leaf-profiles.jsonl", leaf_profiles)
        _write_jsonl(
            temporary / "all-geometric-gap-evidence.jsonl",
            raw_gap_candidates,
        )
        # Preserve the complete evidence above, but make the visual gate
        # finite and high-signal: retain at most one strongest gap per
        # leaf/axis, prioritize known target intersections, then cap at 128.
        best_gap_by_leaf_axis: dict[tuple[str, str], dict[str, object]] = {}
        for candidate in raw_gap_candidates:
            key = (str(candidate["leaf_id"]), str(candidate["axis"]))
            score = float(candidate["gap"]) * float(candidate["pixel_balance"])
            previous = best_gap_by_leaf_axis.get(key)
            if previous is None or score > float(previous["review_score"]):
                best_gap_by_leaf_axis[key] = {
                    **candidate,
                    "review_score": score,
                }
        target_boxes = tuple(
            _box(value["bbox"]) for value in _target_definitions(fixture.name)
        )
        ranked_gaps = sorted(
            best_gap_by_leaf_axis.values(),
            key=lambda value: (
                any(
                    _intersects(_box(value["leaf_bbox"]), target_box)
                    for target_box in target_boxes
                ),
                float(value["review_score"]),
                int(value["gap"]),
            ),
            reverse=True,
        )
        missed_cut_candidates = ranked_gaps[:128]
        _write_jsonl(
            temporary / "missed-cut-candidates.jsonl",
            missed_cut_candidates,
        )
        limit_leaves = [profile for profile in leaf_profiles if profile["terminal_reason"] == "limit"]
        _add_check(
            checks,
            "no-recursion-limit-leaves",
            failures=limit_leaves,
            severity="critical",
            evidence="leaf-profiles.jsonl",
        )
        checks.append(
            AuditCheck(
                check_id="geometric-missed-cut-candidates-require-human-verdict",
                status="REVIEW" if missed_cut_candidates else "PASS",
                severity="review",
                count=len(missed_cut_candidates),
                evidence="all-geometric-gap-evidence.jsonl; missed-cut-candidates.jsonl; suspicious-crop-sheets/",
            )
        )

        # Exhaustive byte/pixel audit of every actual raw and isolated segment
        # crop.  This is independent of contact-sheet thumbnails.
        crop_root = stage / "segment-crops"
        crop_manifest_path = crop_root / "manifest.json"
        crop_failures: list[dict[str, object]] = []
        crop_manifest = _load_json(crop_manifest_path) if crop_manifest_path.is_file() else {}
        crop_items = crop_manifest.get("items") or [] if isinstance(crop_manifest, dict) else []
        item_by_id = {str(item["segment_id"]): item for item in crop_items}
        if len(item_by_id) != len(crop_items):
            crop_failures.append({"kind": "duplicate-crop-manifest-id"})
        for segment in segments:
            segment_id = str(segment["segment_id"])
            item = item_by_id.get(segment_id)
            if item is None:
                crop_failures.append({"segment_id": segment_id, "kind": "missing-manifest-item"})
                continue
            raw_path = crop_root / str(item["raw"])
            isolated_path = crop_root / str(item["isolated"])
            if not raw_path.is_file() or not isolated_path.is_file():
                crop_failures.append({"segment_id": segment_id, "kind": "missing-crop-file"})
                continue
            index = segment_index[segment_id]
            leaf_index = segment_to_leaf.get(segment_id, -1)
            if leaf_index < 0:
                continue
            left, top, right, bottom = _box(segment["bbox"])
            expected_raw = aligned[top:bottom, left:right]
            local_leaf = leaf_raster[top:bottom, left:right]
            expected_mask = np.logical_and(non_rule[top:bottom, left:right], local_leaf == leaf_index)
            expected_isolated = np.full(expected_raw.shape, 255, dtype=np.uint8)
            expected_isolated[expected_mask] = expected_raw[expected_mask]
            actual_raw = _load_rgb(raw_path)
            actual_isolated = _load_rgb(isolated_path)
            mismatch = []
            if not np.array_equal(actual_raw, expected_raw):
                mismatch.append("raw-pixels")
            if not np.array_equal(actual_isolated, expected_isolated):
                mismatch.append("isolated-pixels")
            if str(item.get("raw_sha256")) != _sha256_file(raw_path):
                mismatch.append("raw-sha")
            if str(item.get("isolated_sha256")) != _sha256_file(isolated_path):
                mismatch.append("isolated-sha")
            if int(item.get("ownership_pixels", -1)) != int(segment_pixel_counts[index]):
                mismatch.append("manifest-ownership-count")
            if mismatch:
                crop_failures.append({"segment_id": segment_id, "kind": mismatch})
        unexpected_crop_ids = set(item_by_id) - set(segment_ids)
        crop_failures.extend({"segment_id": value, "kind": "unexpected-manifest-item"} for value in sorted(unexpected_crop_ids))
        _write_jsonl(temporary / "segment-crop-failures.jsonl", crop_failures)
        _add_check(
            checks,
            "all-actual-segment-crops-equal-independent-pixels",
            failures=crop_failures,
            severity="critical",
            evidence="segment-crop-failures.jsonl",
        )

        # Verify and copy every existing segment contact sheet.  The root
        # review can inspect these actual-crop indexes without relying on an
        # overlay of the source page.
        copied_sheet_root = temporary / "segment-contact-sheets"
        copied_sheet_root.mkdir()
        contact_failures: list[dict[str, object]] = []
        covered_indexes: list[int] = []
        contact_values = crop_manifest.get("contact_sheets") or [] if isinstance(crop_manifest, dict) else []
        for relative in contact_values:
            source_sheet = crop_root / str(relative)
            match = _CONTACT_RANGE.fullmatch(source_sheet.name)
            if not source_sheet.is_file() or match is None:
                contact_failures.append({"path": str(relative), "kind": "missing-or-invalid-name"})
                continue
            start, stop = (int(value) for value in match.groups())
            covered_indexes.extend(range(start, stop + 1))
            target_sheet = copied_sheet_root / source_sheet.name
            shutil.copy2(source_sheet, target_sheet)
            with Image.open(target_sheet) as opened:
                if opened.width < 1 or opened.height < 1:
                    contact_failures.append({"path": str(relative), "kind": "empty-image"})
        if sorted(covered_indexes) != list(range(len(segments))):
            contact_failures.append(
                {
                    "kind": "coverage",
                    "covered": len(covered_indexes),
                    "unique": len(set(covered_indexes)),
                    "expected": len(segments),
                }
            )
        _write_jsonl(temporary / "segment-contact-sheet-failures.jsonl", contact_failures)
        _add_check(
            checks,
            "segment-contact-sheets-cover-every-segment-once",
            failures=contact_failures,
            severity="critical",
            evidence="segment-contact-sheet-failures.jsonl; segment-contact-sheets/",
        )

        # Actual raw/isolated matrix pages.  The grid overlay is supplemental.
        matrix_page_root = temporary / "matrix-pixel-pages"
        matrix_page_root.mkdir()
        matrix_pages: list[dict[str, object]] = []
        tile_size = 1024
        for top in range(0, height, tile_size):
            for left in range(0, width, tile_size):
                bottom = min(height, top + tile_size)
                right = min(width, left + tile_size)
                stem = f"y{top:05d}-{bottom:05d}-x{left:05d}-{right:05d}"
                raw_path = matrix_page_root / f"{stem}-raw.png"
                isolated_path = matrix_page_root / f"{stem}-isolated.png"
                overlay_path = matrix_page_root / f"{stem}-grid-overlay.png"
                raw = np.array(aligned[top:bottom, left:right], copy=True)
                isolated = np.full(raw.shape, 255, dtype=np.uint8)
                local_non_rule = non_rule[top:bottom, left:right]
                isolated[local_non_rule] = raw[local_non_rule]
                Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
                Image.fromarray(isolated, mode="RGB").save(isolated_path, format="PNG")
                overlay = Image.fromarray(raw, mode="RGB")
                draw = ImageDraw.Draw(overlay)
                for interval in rows:
                    coordinate = int(interval["start"])
                    if top < coordinate < bottom:
                        draw.line((0, coordinate - top, right - left - 1, coordinate - top), fill=(175, 60, 210), width=1)
                for interval in columns:
                    coordinate = int(interval["start"])
                    if left < coordinate < right:
                        draw.line((coordinate - left, 0, coordinate - left, bottom - top - 1), fill=(175, 60, 210), width=1)
                overlay.save(overlay_path, format="PNG")
                overlay.close()
                matrix_pages.append(
                    {
                        "page_id": stem,
                        "bbox": [left, top, right, bottom],
                        "raw": raw_path.relative_to(temporary).as_posix(),
                        "isolated": isolated_path.relative_to(temporary).as_posix(),
                        "grid_overlay_supplemental": overlay_path.relative_to(temporary).as_posix(),
                        "manual_status": "PENDING",
                    }
                )
        _write_jsonl(temporary / "matrix-pixel-pages.jsonl", matrix_pages)

        # Readable complete matrix pages, grouped by matrix row rather than one
        # enormous uninspectable dump.
        text_page_root = temporary / "matrix-text-pages"
        text_page_root.mkdir()
        cells_by_row: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
        for (row, column, segment_id), pixels in expected_cells.items():
            cells_by_row[row].append((column, segment_id, pixels))
        text_pages: list[str] = []
        page_rows = 40
        for start in range(0, len(rows), page_rows):
            stop = min(len(rows), start + page_rows)
            path = text_page_root / f"rows-{start:05d}-{stop - 1:05d}.txt"
            with path.open("w", encoding="utf-8") as stream:
                for row in range(start, stop):
                    interval = rows[row]
                    stream.write(
                        f"ROW {row} pixels=[{interval['start']}:{interval['end']}) cells={len(cells_by_row[row])}\n"
                    )
                    for column, segment_id, pixels in sorted(cells_by_row[row]):
                        column_interval = columns[column]
                        stream.write(
                            f"  C {column} pixels=[{column_interval['start']}:{column_interval['end']}) "
                            f"{segment_id} owned_pixels={pixels}\n"
                        )
            text_pages.append(path.relative_to(temporary).as_posix())
        _write_json(temporary / "matrix-text-pages.json", text_pages)

        # Deterministic actual cell samples: edges, quantiles, smallest/largest
        # pixel support, collisions, and all cells touching targeted regions.
        sorted_cell_keys = sorted(expected_cells)
        selected_cells: set[tuple[int, int, str]] = set()
        if sorted_cell_keys:
            stride = max(1, len(sorted_cell_keys) // 96)
            selected_cells.update(sorted_cell_keys[::stride][:96])
            selected_cells.update(sorted_cell_keys[:16])
            selected_cells.update(sorted_cell_keys[-16:])
            by_pixels = sorted(sorted_cell_keys, key=lambda key: (expected_cells[key], key))
            selected_cells.update(by_pixels[:24])
            selected_cells.update(by_pixels[-24:])
        coordinate_owners: dict[tuple[int, int], list[str]] = defaultdict(list)
        for row, column, segment_id in sorted_cell_keys:
            coordinate_owners[(row, column)].append(segment_id)
        for coordinate, owners in coordinate_owners.items():
            if len(owners) > 1:
                selected_cells.update((coordinate[0], coordinate[1], owner) for owner in owners)
                if len(selected_cells) >= 240:
                    break
        target_values = _target_definitions(fixture.name)
        for target in target_values:
            target_box = _box(target["bbox"])
            for key in sorted_cell_keys:
                row, column, _ = key
                cell_box = (
                    int(columns[column]["start"]),
                    int(rows[row]["start"]),
                    int(columns[column]["end"]),
                    int(rows[row]["end"]),
                )
                if _intersects(cell_box, target_box):
                    selected_cells.add(key)
        if len(selected_cells) > 320:
            selected_cells = set(sorted(selected_cells)[:320])

        cell_crop_root = temporary / "cell-samples"
        raw_cell_root = cell_crop_root / "raw"
        isolated_cell_root = cell_crop_root / "isolated"
        sheet_cell_root = cell_crop_root / "contact-sheets"
        raw_cell_root.mkdir(parents=True)
        isolated_cell_root.mkdir()
        sheet_cell_root.mkdir()
        cell_sheet_items: list[tuple[str, Path, Path]] = []
        cell_sample_manifest: list[dict[str, object]] = []
        for sample_index, (row, column, segment_id) in enumerate(sorted(selected_cells)):
            left = int(columns[column]["start"])
            right = int(columns[column]["end"])
            top = int(rows[row]["start"])
            bottom = int(rows[row]["end"])
            raw = np.array(aligned[top:bottom, left:right], copy=True)
            isolated = np.full(raw.shape, 255, dtype=np.uint8)
            leaf_index = segment_to_leaf[segment_id]
            exact = np.logical_and(
                non_rule[top:bottom, left:right],
                leaf_raster[top:bottom, left:right] == leaf_index,
            )
            isolated[exact] = raw[exact]
            stem = f"cell-{sample_index:04d}-r{row:05d}-c{column:05d}-{segment_id}"
            raw_path = raw_cell_root / f"{stem}.png"
            isolated_path = isolated_cell_root / f"{stem}.png"
            Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
            Image.fromarray(isolated, mode="RGB").save(isolated_path, format="PNG")
            label = f"r{row} c{column} {segment_id} px={expected_cells[(row, column, segment_id)]}"
            cell_sheet_items.append((label, raw_path, isolated_path))
            cell_sample_manifest.append(
                {
                    "row": row,
                    "column": column,
                    "segment_id": segment_id,
                    "bbox": [left, top, right, bottom],
                    "owned_pixels": expected_cells[(row, column, segment_id)],
                    "raw": raw_path.relative_to(temporary).as_posix(),
                    "isolated": isolated_path.relative_to(temporary).as_posix(),
                }
            )
        cell_sheet_paths: list[str] = []
        for start in range(0, len(cell_sheet_items), 24):
            stop = min(len(cell_sheet_items), start + 24)
            path = sheet_cell_root / f"cells-{start:04d}-{stop - 1:04d}.png"
            _render_pair_sheet(path, items=cell_sheet_items, start=start, stop=stop)
            cell_sheet_paths.append(path.relative_to(temporary).as_posix())
        _write_json(
            cell_crop_root / "manifest.json",
            {"items": cell_sample_manifest, "contact_sheets": cell_sheet_paths},
        )

        # Target-region actual pixels and exact intersecting IDs.
        target_root = temporary / "target-regions"
        target_root.mkdir()
        target_findings: list[dict[str, object]] = []
        for target in target_values:
            target_id = str(target["target_id"])
            left, top, right, bottom = _box(target["bbox"])
            left, right = max(0, left), min(width, right)
            top, bottom = max(0, top), min(height, bottom)
            raw = np.array(aligned[top:bottom, left:right], copy=True)
            segment_isolated = np.full(raw.shape, 255, dtype=np.uint8)
            local_non_rule = non_rule[top:bottom, left:right]
            segment_isolated[local_non_rule] = raw[local_non_rule]
            rule_isolated = np.full(raw.shape, 255, dtype=np.uint8)
            local_rule = rule_mask[top:bottom, left:right]
            rule_isolated[local_rule] = raw[local_rule]
            raw_path = target_root / f"{target_id}-raw.png"
            segment_path = target_root / f"{target_id}-segments-isolated.png"
            rule_path = target_root / f"{target_id}-rules-isolated.png"
            Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
            Image.fromarray(segment_isolated, mode="RGB").save(segment_path, format="PNG")
            Image.fromarray(rule_isolated, mode="RGB").save(rule_path, format="PNG")
            target_box = (left, top, right, bottom)
            intersecting_segments = [
                str(segment["segment_id"])
                for segment in segments
                if _intersects(_box(segment["bbox"]), target_box)
            ]
            intersecting_rules = [
                str(rule["rule_id"])
                for rule in rules
                if _intersects(_box(rule["bbox"]), target_box)
            ]
            intersecting_leaves = [
                str(leaf["node_id"])
                for leaf in leaves
                if _intersects(_box(leaf["bbox"]), target_box)
            ]
            target_findings.append(
                {
                    **target,
                    "clamped_bbox": [left, top, right, bottom],
                    "foreground_pixels": int(np.count_nonzero(foreground[top:bottom, left:right])),
                    "segment_owned_pixels": int(np.count_nonzero(local_non_rule)),
                    "rule_owned_pixels": int(np.count_nonzero(local_rule)),
                    "intersecting_segment_ids": intersecting_segments,
                    "intersecting_rule_ids": intersecting_rules,
                    "intersecting_leaf_ids": intersecting_leaves,
                    "raw": raw_path.relative_to(temporary).as_posix(),
                    "segments_isolated": segment_path.relative_to(temporary).as_posix(),
                    "rules_isolated": rule_path.relative_to(temporary).as_posix(),
                    "manual_status": "PENDING",
                }
            )
        _write_jsonl(temporary / "target-regions.jsonl", target_findings)

        # Full-resolution actual crops for every curated gap candidate and
        # every connected component crossed by a coordinate cut.  Contact
        # sheets are only indexes; raw/isolated files remain primary evidence.
        suspicious_root = temporary / "suspicious-crops"
        suspicious_raw_root = suspicious_root / "raw"
        suspicious_isolated_root = suspicious_root / "isolated"
        suspicious_sheet_root = suspicious_root / "contact-sheets"
        suspicious_raw_root.mkdir(parents=True)
        suspicious_isolated_root.mkdir()
        suspicious_sheet_root.mkdir()
        suspicious_items: list[tuple[str, Path, Path]] = []
        suspicious_manifest: list[dict[str, object]] = []
        for candidate_index, candidate in enumerate(missed_cut_candidates):
            left, top, right, bottom = _box(candidate["leaf_bbox"])
            raw = np.array(aligned[top:bottom, left:right], copy=True)
            isolated = np.full(raw.shape, 255, dtype=np.uint8)
            exact = non_rule[top:bottom, left:right]
            isolated[exact] = raw[exact]
            stem = f"gap-{candidate_index:04d}"
            raw_path = suspicious_raw_root / f"{stem}.png"
            isolated_path = suspicious_isolated_root / f"{stem}.png"
            Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
            Image.fromarray(isolated, mode="RGB").save(isolated_path, format="PNG")
            label = (
                f"gap {candidate_index} {candidate['axis']} band={candidate['band']} "
                f"score={float(candidate['review_score']):.2f}"
            )
            suspicious_items.append((label, raw_path, isolated_path))
            suspicious_manifest.append(
                {
                    "kind": "missed-cut-candidate",
                    "index": candidate_index,
                    "bbox": [left, top, right, bottom],
                    "candidate": candidate,
                    "raw": raw_path.relative_to(temporary).as_posix(),
                    "isolated": isolated_path.relative_to(temporary).as_posix(),
                }
            )
        for component_index, component in enumerate(cut_components):
            left, top, right, bottom = _box(component["bbox"])
            padding = 12
            left = max(0, left - padding)
            top = max(0, top - padding)
            right = min(width, right + padding)
            bottom = min(height, bottom + padding)
            raw = np.array(aligned[top:bottom, left:right], copy=True)
            isolated = np.full(raw.shape, 255, dtype=np.uint8)
            label_value = int(component["component_label"])
            exact = component_labels[top:bottom, left:right] == label_value
            isolated[exact] = raw[exact]
            stem = f"cut-component-{component_index:04d}"
            raw_path = suspicious_raw_root / f"{stem}.png"
            isolated_path = suspicious_isolated_root / f"{stem}.png"
            Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
            Image.fromarray(isolated, mode="RGB").save(isolated_path, format="PNG")
            label = (
                f"cut component {label_value} leaves={component['leaf_ids']}"
            )
            suspicious_items.append((label, raw_path, isolated_path))
            suspicious_manifest.append(
                {
                    "kind": "coordinate-cut-component",
                    "index": component_index,
                    "bbox_with_padding": [left, top, right, bottom],
                    "component": component,
                    "raw": raw_path.relative_to(temporary).as_posix(),
                    "isolated": isolated_path.relative_to(temporary).as_posix(),
                }
            )
        suspicious_sheet_paths: list[str] = []
        for start in range(0, len(suspicious_items), 24):
            stop = min(len(suspicious_items), start + 24)
            path = suspicious_sheet_root / f"suspicious-{start:04d}-{stop - 1:04d}.png"
            _render_pair_sheet(
                path,
                items=suspicious_items,
                start=start,
                stop=stop,
            )
            suspicious_sheet_paths.append(path.relative_to(temporary).as_posix())
        _write_json(
            suspicious_root / "manifest.json",
            {
                "items": suspicious_manifest,
                "contact_sheets": suspicious_sheet_paths,
            },
        )

        edge_rules: list[dict[str, object]] = []
        for rule in rules:
            left, top, right, bottom = _box(rule["bbox"])
            horizontal_long = right - left >= 0.8 * width
            vertical_long = bottom - top >= 0.8 * height
            at_edge = left <= 3 or top <= 3 or right >= width - 3 or bottom >= height - 3
            if at_edge and (horizontal_long or vertical_long):
                edge_rules.append(
                    {
                        "rule_id": rule["rule_id"],
                        "bbox": [left, top, right, bottom],
                        "axis": rule["axis"],
                        "foreground_pixels": rule["foreground_pixels"],
                    }
                )
        _write_jsonl(temporary / "page-frame-rule-candidates.jsonl", edge_rules)
        checks.append(
            AuditCheck(
                check_id="page-frame-rule-leakage-manual-review",
                status="REVIEW" if edge_rules else "PASS",
                severity="review",
                count=len(edge_rules),
                evidence="page-frame-rule-candidates.jsonl; matrix-pixel-pages",
            )
        )

        _write_jsonl(
            temporary / "checks.jsonl",
            (
                {
                    "check_id": check.check_id,
                    "status": check.status,
                    "severity": check.severity,
                    "count": check.count,
                    "evidence": check.evidence,
                }
                for check in checks
            ),
        )
        critical_failures = [
            check for check in checks if check.severity == "critical" and check.status == "FAIL"
        ]
        automated_verdict = "REJECT" if critical_failures else "MANUAL_REVIEW_PENDING"
        (temporary / "verdict.txt").write_text(automated_verdict + "\n", encoding="utf-8")

        # Explicit manual ledger.  No automated PASS can fill these rows.
        with (temporary / "manual-audit.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("kind", "item", "bbox_or_range", "manual_status", "notes"))
            for page in matrix_pages:
                writer.writerow(("matrix-pixel-page", page["page_id"], page["bbox"], "PENDING", "review raw + isolated; overlay supplemental only"))
            for path in text_pages:
                writer.writerow(("matrix-text-page", path, "", "PENDING", "verify readable row/cell mapping"))
            for path in cell_sheet_paths:
                writer.writerow(("cell-sample-sheet", path, "", "PENDING", "review actual raw + isolated cells"))
            for path in suspicious_sheet_paths:
                writer.writerow(("suspicious-crop-sheet", path, "", "PENDING", "review actual raw + isolated gap/cut-component crops"))
            for target in target_findings:
                writer.writerow(("target-region", target["target_id"], target["clamped_bbox"], "PENDING", target["note"]))

        shutil.copy2(Path(__file__), temporary / "auditor-source.py")
        source_files = [
            stage / name for name in required
        ] + [crop_manifest_path]
        provenance = {
            "schema": "stage1-independent-pixel-audit-v1",
            "fixture": str(fixture.resolve()),
            "fixture_file_sha256": _sha256_file(fixture),
            "fixture_pixel_sha256": _sha256_pixels(source_fixture),
            "geometry_stage": str(stage.resolve()),
            "geometry_manifest_file_sha256": _sha256_file(stage / "manifest.json"),
            "auditor_source_sha256": _sha256_file(temporary / "auditor-source.py"),
            "automated_verdict": automated_verdict,
            "manual_verdict": "PENDING",
            "counts": {
                "nodes": len(nodes),
                "leaves": len(leaves),
                "segments": len(segments),
                "rules": len(rules),
                "independent_connected_components": component_count - 1,
                "matrix_rows": len(rows),
                "matrix_columns": len(columns),
                "matrix_cells": len(cells),
                "matrix_pixel_pages": len(matrix_pages),
                "matrix_text_pages": len(text_pages),
                "cell_samples": len(cell_sample_manifest),
                "all_geometric_gap_evidence": len(raw_gap_candidates),
                "missed_cut_review_candidates": len(missed_cut_candidates),
                "coordinate_cut_components_for_review": len(cut_components),
                "suspicious_crop_sheets": len(suspicious_sheet_paths),
            },
            "source_files": [
                {"path": str(path.resolve()), "sha256": _sha256_file(path)}
                for path in source_files
                if path.is_file()
            ],
            "method": {
                "ownership": "terminal leaf rectangles + foreground minus rule mask",
                "components": f"OpenCV {cv2.__version__} connectedComponentsWithStats, 8-connectivity",
                "sparse_cells": "recomputed from every independently owned pixel",
                "production_imports": False,
                "semantic_inference": False,
                "overlay_is_primary_evidence": False,
            },
        }
        _write_json(temporary / "manifest.json", provenance)

        report_lines = [
            "# Independent Stage 1 grid/matrix pixel audit",
            "",
            f"Automated verdict: **{automated_verdict}**. Manual verdict: **PENDING**.",
            "",
            "This audit is deliberately nonsemantic: bbox size, font size, words, and header status are not error criteria. A rule/underline distinction is not inferred from aspect ratio alone.",
            "",
            "## Checks",
            "",
            "| check | status | severity | count | evidence |",
            "|---|---:|---:|---:|---|",
        ]
        report_lines.extend(
            f"| `{check.check_id}` | {check.status} | {check.severity} | {check.count} | {check.evidence} |"
            for check in checks
        )
        report_lines.extend(
            (
                "",
                "## Manual gate",
                "",
                "Review and fill `manual-audit.tsv`. Matrix pages use actual raw and isolated pixels; grid overlays are supplemental. A final PASS is forbidden while any required row remains PENDING.",
                "",
                "## Key evidence",
                "",
                "- `tree.tsv`: every recursive node, cut, children, and terminal reason.",
                "- `grid-boundaries.tsv`: every sparse axis interval with its pixel-evidence sources.",
                "- `matrix-cells.jsonl`: every independently reconstructed nonzero cell and owned-pixel count.",
                "- `matrix-pixel-pages/`: actual page tiles, raw and isolated; overlays are separate.",
                "- `segment-contact-sheets/`: copied actual raw/isolated segment indexes.",
                "- `target-regions/`: actual evidence for old361/merged/weak-H/old758 regions when applicable.",
            )
        )
        (temporary / "audit.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        # Hash output evidence except the manifest itself (avoids a circular
        # hash) and large copied contact sheets are included.
        evidence_rows = []
        for path in sorted(value for value in temporary.rglob("*") if value.is_file() and value.name != "manifest.json"):
            evidence_rows.append(
                {
                    "path": path.relative_to(temporary).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
        _write_jsonl(temporary / "evidence-files.jsonl", evidence_rows)

        del component_labels, leaf_raster
        temporary.rename(final)
        return final
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--geometry-run", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("debug/labs"))
    parser.add_argument("--run-id")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.fixture is None or args.geometry_run is None or args.run_id is None:
        raise SystemExit("--fixture, --geometry-run and --run-id are required")
    path = audit(
        fixture=args.fixture,
        geometry_run=args.geometry_run,
        output_root=args.output_root,
        run_id=args.run_id,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
