#!/usr/bin/env python3
"""Validate v8/v16 recursive-grid artifacts as an external geometry oracle.

Only actual source crops and machine-readable traces are primary evidence.
Legacy overlays and crop sheets are copied for navigation but never certify a
run.  The audit is deliberately nonsemantic.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rgb(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return np.asarray(opened.convert("RGB"), dtype=np.uint8)


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _runs(values: np.ndarray) -> tuple[tuple[int, int], ...]:
    indexes = np.flatnonzero(values)
    if indexes.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(indexes) > 1)
    starts = np.concatenate((indexes[:1], indexes[breaks + 1]))
    stops = np.concatenate((indexes[breaks] + 1, indexes[-1:] + 1))
    return tuple((int(start), int(stop)) for start, stop in zip(starts, stops))


def _foreground(image: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int]]:
    height, width = image.shape[:2]
    border_width = max(1, min(16, height // 20, width // 20))
    border = np.concatenate(
        (
            image[:border_width].reshape(-1, 3),
            image[-border_width:].reshape(-1, 3),
            image[:, :border_width].reshape(-1, 3),
            image[:, -border_width:].reshape(-1, 3),
        ),
        axis=0,
    )
    background_array = np.median(border, axis=0).astype(np.int16)
    delta = np.max(np.abs(image.astype(np.int16) - background_array), axis=2)
    return delta >= 24, tuple(int(value) for value in background_array)


def _quantiles(values: Sequence[int]) -> dict[str, float]:
    if not values:
        return {"min": 0, "p25": 0, "median": 0, "p75": 0, "max": 0}
    array = np.asarray(values, dtype=np.float64)
    return {
        "min": float(array.min()),
        "p25": float(np.quantile(array, 0.25)),
        "median": float(np.quantile(array, 0.5)),
        "p75": float(np.quantile(array, 0.75)),
        "max": float(array.max()),
    }


def _render_sheet(
    path: Path,
    items: Sequence[tuple[str, Path]],
    *,
    start: int,
    stop: int,
) -> None:
    subset = items[start:stop]
    tile_width, tile_height, columns = 480, 240, 3
    rows = max(1, math.ceil(len(subset) / columns))
    sheet = Image.new("RGB", (tile_width * columns, tile_height * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        for offset, (label, source) in enumerate(subset):
            column = offset % columns
            row = offset // columns
            left = column * tile_width
            top = row * tile_height
            draw.rectangle(
                (left, top, left + tile_width - 1, top + tile_height - 1),
                outline=(175, 175, 175),
            )
            draw.text((left + 6, top + 6), label[:76], fill=(0, 0, 0))
            with Image.open(source) as opened:
                preview = opened.convert("RGB")
            try:
                preview.thumbnail(
                    (tile_width - 16, tile_height - 38),
                    Image.Resampling.NEAREST,
                )
                sheet.paste(
                    preview,
                    (
                        left + (tile_width - preview.width) // 2,
                        top + 30 + (tile_height - 34 - preview.height) // 2,
                    ),
                )
            finally:
                preview.close()
        sheet.save(path, format="PNG")
    finally:
        sheet.close()


def _render_pair_sheet(
    path: Path,
    items: Sequence[tuple[str, Path, Path]],
    *,
    start: int,
    stop: int,
) -> None:
    subset = items[start:stop]
    tile_width, tile_height, columns = 480, 240, 3
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
            draw.text((left + 6, top + 6), label[:76], fill=(0, 0, 0))
            for side, source in enumerate((raw_path, isolated_path)):
                with Image.open(source) as opened:
                    preview = opened.convert("RGB")
                try:
                    preview.thumbnail(
                        (tile_width // 2 - 16, tile_height - 38),
                        Image.Resampling.NEAREST,
                    )
                    sheet.paste(
                        preview,
                        (
                            left
                            + side * (tile_width // 2)
                            + (tile_width // 2 - preview.width) // 2,
                            top + 30 + (tile_height - 34 - preview.height) // 2,
                        ),
                    )
                finally:
                    preview.close()
        sheet.save(path, format="PNG")
    finally:
        sheet.close()


def _validate_version_fixture(
    *,
    version: str,
    fixture_name: str,
    version_root: Path,
    destination: Path,
) -> dict[str, object]:
    source_dir = version_root / fixture_name
    recursion_path = source_dir / "recursion.json"
    matrix_path = source_dir / "matrix.tsv"
    if not recursion_path.is_file() or not matrix_path.is_file():
        raise FileNotFoundError(f"missing {version} artifact for {fixture_name}")
    recursion = _json(recursion_path)
    if not isinstance(recursion, dict):
        raise ValueError("recursion.json must contain an object")
    fixture = Path(str(recursion["source"]))
    if not fixture.is_file():
        raise FileNotFoundError(fixture)
    image = _rgb(fixture)
    height, width = image.shape[:2]
    foreground, background = _foreground(image)
    leaves = recursion.get("leaves") or []
    anchors = recursion.get("anchors") or []
    codes = recursion.get("codes") or []
    groups = recursion.get("groups") or []
    shape = recursion.get("sparse_shape") or []
    tracks = recursion.get("x_tracks") or []
    failures: list[dict[str, object]] = []

    if recursion.get("source_sha256") != _sha(fixture):
        failures.append({"kind": "source-sha"})
    if int(recursion.get("leaf_count", -1)) != len(leaves):
        failures.append({"kind": "leaf-count"})
    if int(recursion.get("group_count", -1)) != len(groups):
        failures.append({"kind": "group-count"})
    if len(shape) != 2 or min(int(value) for value in shape) < 1:
        failures.append({"kind": "sparse-shape"})
        sparse_rows, sparse_columns = 0, 0
    else:
        sparse_rows, sparse_columns = (int(value) for value in shape)
    if len(tracks) != sparse_columns or list(tracks) != sorted(set(tracks)):
        failures.append(
            {
                "kind": "x-tracks",
                "track_count": len(tracks),
                "sparse_columns": sparse_columns,
            }
        )
    if len(anchors) != len(leaves):
        failures.append({"kind": "anchor-count"})
    invalid_anchors = [
        {"leaf": index, "anchor": anchor}
        for index, anchor in enumerate(anchors)
        if len(anchor) != 2
        or not (0 <= int(anchor[0]) < sparse_rows)
        or not (0 <= int(anchor[1]) < sparse_columns)
    ]
    failures.extend({"kind": "invalid-anchor", **value} for value in invalid_anchors)
    invalid_codes = [
        code
        for code in codes
        if len(code) != 3
        or not (0 <= int(code[0]) < sparse_rows)
        or not (0 <= int(code[1]) < sparse_columns)
    ]
    failures.extend({"kind": "invalid-code", "code": value} for value in invalid_codes)
    if len({tuple(int(item) for item in code) for code in codes}) != len(codes):
        failures.append({"kind": "duplicate-global-code"})

    coverage = np.zeros((height, width), dtype=np.uint16)
    crop_failures: list[dict[str, object]] = []
    multirow: list[dict[str, object]] = []
    leaf_ledger: list[dict[str, object]] = []
    leaf_heights: list[int] = []
    content_heights: list[int] = []
    all_leaf_codes: list[tuple[int, int, int]] = []
    leaf_indexes_seen: set[int] = set()
    version_destination = destination / version / fixture_name
    crop_destination = version_destination / "source-crops"
    crop_destination.mkdir(parents=True)

    for expected_index, leaf in enumerate(leaves):
        index = int(leaf.get("index", expected_index))
        if index != expected_index:
            failures.append({"kind": "leaf-index", "expected": expected_index, "actual": index})
        left, top, right, bottom = (int(value) for value in leaf["source_bbox"])
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            failures.append({"kind": "leaf-bbox", "leaf": index, "bbox": leaf["source_bbox"]})
            continue
        coverage[top:bottom, left:right] += 1
        expected_crop = image[top:bottom, left:right]
        source_crop_path = source_dir / str(leaf["source_crop"])
        if not source_crop_path.is_file():
            crop_failures.append({"leaf": index, "kind": "missing-source-crop"})
            continue
        actual_crop = _rgb(source_crop_path)
        mismatch = []
        if not np.array_equal(actual_crop, expected_crop):
            mismatch.append("pixels")
        if leaf.get("source_crop_sha256") != _sha(source_crop_path):
            mismatch.append("sha")
        if mismatch:
            crop_failures.append({"leaf": index, "kind": mismatch})
        copied_crop = crop_destination / f"leaf-{index:06d}.png"
        shutil.copy2(source_crop_path, copied_crop)

        leaf_mask = foreground[top:bottom, left:right]
        projection = leaf_mask.any(axis=1)
        internal_gaps = [
            [top + start, top + stop]
            for start, stop in _runs(np.logical_not(projection))
            if start > 0
            and stop < projection.size
            and stop - start >= 8
            and np.any(projection[:start])
            and np.any(projection[stop:])
        ]
        if internal_gaps:
            multirow.append(
                {
                    "leaf": index,
                    "source_bbox": [left, top, right, bottom],
                    "internal_blank_bands_at_least_8px": internal_gaps,
                    "separable_row_regions": len(internal_gaps) + 1,
                    "actual_source_crop": copied_crop.relative_to(destination).as_posix(),
                    "interpretation": "contextual multi-region crop; size alone is not an error",
                }
            )
        leaf_ledger.append(
            {
                "leaf": index,
                "source_bbox": [left, top, right, bottom],
                "content_bbox": leaf.get("content_bbox"),
                "height": bottom - top,
                "width": right - left,
                "foreground_pixels": int(np.count_nonzero(leaf_mask)),
                "internal_blank_bands_at_least_8px": internal_gaps,
                "anchor": leaf.get("anchor"),
                "codes": leaf.get("codes") or [],
                "actual_source_crop": copied_crop.relative_to(destination).as_posix(),
            }
        )
        leaf_heights.append(bottom - top)
        content = leaf.get("content_bbox")
        if content:
            content_heights.append(int(content[3]) - int(content[1]))
        leaf_anchor = tuple(int(value) for value in leaf.get("anchor") or [])
        if len(leaf_anchor) != 2 or expected_index >= len(anchors) or leaf_anchor != tuple(int(value) for value in anchors[expected_index]):
            failures.append({"kind": "leaf-anchor-disagrees", "leaf": index})
        leaf_codes = [tuple(int(value) for value in code) for code in leaf.get("codes") or []]
        all_leaf_codes.extend(leaf_codes)

    global_codes = {tuple(int(value) for value in code) for code in codes}
    if set(all_leaf_codes) != global_codes:
        failures.append(
            {
                "kind": "leaf-global-code-set-disagrees",
                "missing": len(global_codes - set(all_leaf_codes)),
                "extra": len(set(all_leaf_codes) - global_codes),
            }
        )

    for expected_group, group in enumerate(groups):
        if int(group.get("index", -1)) != expected_group:
            failures.append({"kind": "group-index", "group": expected_group})
        group_leaves = [int(value) for value in group.get("leaf_indexes") or []]
        leaf_indexes_seen.update(group_leaves)
        if any(value < 0 or value >= len(leaves) for value in group_leaves):
            failures.append({"kind": "group-leaf-range", "group": expected_group})
            continue
        if group_leaves:
            boxes = [leaves[value]["source_bbox"] for value in group_leaves]
            union = [
                min(int(box[0]) for box in boxes),
                min(int(box[1]) for box in boxes),
                max(int(box[2]) for box in boxes),
                max(int(box[3]) for box in boxes),
            ]
            if [int(value) for value in group["bbox"]] != union:
                failures.append(
                    {"kind": "group-bbox", "group": expected_group, "declared": group["bbox"], "union": union}
                )
    if leaf_indexes_seen != set(range(len(leaves))):
        failures.append(
            {
                "kind": "group-leaf-coverage",
                "missing": sorted(set(range(len(leaves))) - leaf_indexes_seen),
                "extra": sorted(leaf_indexes_seen - set(range(len(leaves)))),
            }
        )

    # Parse the external human-readable matrix and ensure it has exactly one
    # row per leaf.  Machine truth remains recursion.json.
    matrix_lines = matrix_path.read_text(encoding="utf-8").splitlines()
    if len(matrix_lines) != len(leaves) + 1:
        failures.append(
            {"kind": "matrix-tsv-row-count", "actual": len(matrix_lines) - 1, "expected": len(leaves)}
        )

    uncovered_foreground = np.logical_and(foreground, coverage == 0)
    duplicate_foreground = np.logical_and(foreground, coverage > 1)
    foreground_pixels = int(np.count_nonzero(foreground))
    uncovered_pixels = int(np.count_nonzero(uncovered_foreground))
    duplicate_pixels = int(np.count_nonzero(duplicate_foreground))
    coverage_rate = (
        1.0 - uncovered_pixels / foreground_pixels if foreground_pixels else 1.0
    )
    coverage_findings = {
        "foreground_definition": "max RGB distance >=24 from median 16px page border",
        "estimated_background_rgb": background,
        "foreground_pixels": foreground_pixels,
        "covered_foreground_pixels": foreground_pixels - uncovered_pixels,
        "coverage_rate": coverage_rate,
        "uncovered_foreground_pixels": uncovered_pixels,
        "duplicated_foreground_pixels": duplicate_pixels,
        "duplicated_foreground_rate": duplicate_pixels / foreground_pixels if foreground_pixels else 0.0,
        "maximum_bbox_multiplicity": int(coverage.max()),
        "interpretation": "overlap is expected for contextual chunks; uncovered foreground is review evidence",
    }

    coverage_image = np.array(image, copy=True)
    coverage_image[uncovered_foreground] = (255, 0, 255)
    coverage_image[duplicate_foreground] = (0, 170, 255)
    Image.fromarray(coverage_image, mode="RGB").save(
        version_destination / "coverage-overlay-supplemental.png",
        format="PNG",
    )
    Image.fromarray(image, mode="RGB").save(
        version_destination / "source-actual.png",
        format="PNG",
    )
    uncovered_root = version_destination / "uncovered-foreground-evidence"
    uncovered_raw_root = uncovered_root / "raw"
    uncovered_isolated_root = uncovered_root / "isolated"
    uncovered_sheet_root = uncovered_root / "contact-sheets"
    uncovered_raw_root.mkdir(parents=True)
    uncovered_isolated_root.mkdir()
    uncovered_sheet_root.mkdir()
    uncovered_items: list[tuple[str, Path, Path]] = []
    uncovered_tiles: list[dict[str, object]] = []
    tile_size = 1024
    for tile_top in range(0, height, tile_size):
        for tile_left in range(0, width, tile_size):
            tile_bottom = min(height, tile_top + tile_size)
            tile_right = min(width, tile_left + tile_size)
            exact = uncovered_foreground[
                tile_top:tile_bottom,
                tile_left:tile_right,
            ]
            pixels = int(np.count_nonzero(exact))
            if not pixels:
                continue
            raw = np.array(
                image[tile_top:tile_bottom, tile_left:tile_right], copy=True
            )
            isolated = np.full(raw.shape, 255, dtype=np.uint8)
            isolated[exact] = raw[exact]
            stem = (
                f"y{tile_top:05d}-{tile_bottom:05d}-"
                f"x{tile_left:05d}-{tile_right:05d}"
            )
            raw_path = uncovered_raw_root / f"{stem}.png"
            isolated_path = uncovered_isolated_root / f"{stem}.png"
            Image.fromarray(raw, mode="RGB").save(raw_path, format="PNG")
            Image.fromarray(isolated, mode="RGB").save(
                isolated_path, format="PNG"
            )
            uncovered_items.append(
                (f"{stem} uncovered={pixels}", raw_path, isolated_path)
            )
            uncovered_tiles.append(
                {
                    "bbox": [tile_left, tile_top, tile_right, tile_bottom],
                    "uncovered_foreground_pixels": pixels,
                    "raw": raw_path.relative_to(destination).as_posix(),
                    "isolated": isolated_path.relative_to(destination).as_posix(),
                }
            )
    uncovered_sheet_paths: list[str] = []
    for start in range(0, len(uncovered_items), 24):
        stop = min(len(uncovered_items), start + 24)
        path = uncovered_sheet_root / f"uncovered-{start:04d}-{stop - 1:04d}.png"
        _render_pair_sheet(
            path,
            uncovered_items,
            start=start,
            stop=stop,
        )
        uncovered_sheet_paths.append(path.relative_to(destination).as_posix())
    _write_json(
        uncovered_root / "manifest.json",
        {"tiles": uncovered_tiles, "contact_sheets": uncovered_sheet_paths},
    )
    existing_sheet = source_dir / "source-crops-sheet.png"
    if existing_sheet.is_file():
        shutil.copy2(existing_sheet, version_destination / "legacy-source-crops-sheet-navigation-only.png")
    shutil.copy2(recursion_path, version_destination / "recursion.json")
    shutil.copy2(matrix_path, version_destination / "matrix.tsv")
    _write_jsonl(version_destination / "crop-failures.jsonl", crop_failures)
    _write_jsonl(version_destination / "multirow-context-leaves.jsonl", multirow)
    _write_jsonl(version_destination / "leaf-ledger.jsonl", leaf_ledger)
    with (version_destination / "leaf-ledger.tsv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(
            (
                "leaf",
                "source_bbox",
                "content_bbox",
                "size",
                "foreground_pixels",
                "internal_blank_bands_at_least_8px",
                "anchor",
                "codes",
                "actual_source_crop",
            )
        )
        for value in leaf_ledger:
            writer.writerow(
                (
                    value["leaf"],
                    value["source_bbox"],
                    value["content_bbox"],
                    f"{value['width']}x{value['height']}",
                    value["foreground_pixels"],
                    value["internal_blank_bands_at_least_8px"],
                    value["anchor"],
                    value["codes"],
                    value["actual_source_crop"],
                )
            )
    _write_json(version_destination / "coverage.json", coverage_findings)
    _write_jsonl(version_destination / "structural-failures.jsonl", failures)

    return {
        "version": version,
        "fixture": fixture_name,
        "fixture_path": str(fixture.resolve()),
        "fixture_sha256": _sha(fixture),
        "recursion_sha256": _sha(recursion_path),
        "matrix_sha256": _sha(matrix_path),
        "leaf_count": len(leaves),
        "group_count": len(groups),
        "sparse_shape": [sparse_rows, sparse_columns],
        "x_track_count": len(tracks),
        "global_code_count": len(codes),
        "leaf_height_quantiles": _quantiles(leaf_heights),
        "content_height_quantiles": _quantiles(content_heights),
        "multirow_context_leaf_count": len(multirow),
        "uncovered_foreground_evidence_sheets": uncovered_sheet_paths,
        "coverage": coverage_findings,
        "actual_source_crop_failures": len(crop_failures),
        "structural_failures": len(failures),
        "destination": version_destination.relative_to(destination).as_posix(),
    }


def audit(
    *,
    v8_root: Path,
    v16_root: Path,
    trace_root: Path,
    output_root: Path,
    run_id: str,
) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("unsafe run id")
    for root in (v8_root, v16_root):
        if not (root / "manifest.json").is_file():
            raise FileNotFoundError(root / "manifest.json")
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(final)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        v8_manifest = _json(v8_root / "manifest.json")
        v16_manifest = _json(v16_root / "manifest.json")
        if not isinstance(v8_manifest, dict) or not isinstance(v16_manifest, dict):
            raise ValueError("legacy manifests must be objects")
        fixture_names = sorted(
            set(path.name for path in v8_root.iterdir() if path.is_dir())
            & set(path.name for path in v16_root.iterdir() if path.is_dir())
        )
        summaries: list[dict[str, object]] = []
        for fixture_name in fixture_names:
            summaries.append(
                _validate_version_fixture(
                    version="v8",
                    fixture_name=fixture_name,
                    version_root=v8_root,
                    destination=temporary,
                )
            )
            summaries.append(
                _validate_version_fixture(
                    version="v16",
                    fixture_name=fixture_name,
                    version_root=v16_root,
                    destination=temporary,
                )
            )

        # The official v16 trace is a second serialization of the external
        # oracle.  Compare all geometry-bearing values, not its overlay PNG.
        official_failures: list[dict[str, object]] = []
        trace_mapping = {
            "000041301_UchebPlan_sign000029629.pdf.raster.png": trace_root / "official-trace-000041",
            "09.03.03_05(ИУ1).pdf.raster.png": trace_root / "official-trace-09",
        }
        official_destination = temporary / "official-v16-traces"
        official_destination.mkdir()
        for fixture_name, trace_dir in trace_mapping.items():
            if fixture_name not in fixture_names:
                continue
            trace_path = trace_dir / "02-recursive-grid-trace.json"
            summary_path = trace_dir / "00-summary.json"
            if not trace_path.is_file() or not summary_path.is_file():
                official_failures.append({"fixture": fixture_name, "kind": "missing-official-trace"})
                continue
            trace = _json(trace_path)
            recursion = _json(v16_root / fixture_name / "recursion.json")
            if not isinstance(trace, dict) or not isinstance(recursion, dict):
                raise ValueError("trace and recursion must be objects")
            comparisons = {
                "profile": (trace.get("profile"), recursion.get("profile")),
                "x_tracks": (trace.get("x_tracks"), recursion.get("x_tracks")),
                "codes": (trace.get("codes"), recursion.get("codes")),
                "leaf_count": (len(trace.get("leaves") or []), len(recursion.get("leaves") or [])),
                "sparse_shape": (
                    [int(trace.get("rows") or 0), int(trace.get("cols") or 0)],
                    recursion.get("sparse_shape"),
                ),
            }
            for key, (first, second) in comparisons.items():
                if first != second:
                    official_failures.append({"fixture": fixture_name, "kind": key})
            trace_leaves = trace.get("leaves") or []
            recursion_leaves = recursion.get("leaves") or []
            for index, (trace_leaf, recursion_leaf) in enumerate(zip(trace_leaves, recursion_leaves)):
                for key in (
                    "source_bbox",
                    "content_bbox",
                    "left_tracks",
                    "dash_track",
                    "merge_left_tracks",
                    "decisions",
                ):
                    if trace_leaf.get(key) != recursion_leaf.get(key):
                        official_failures.append(
                            {"fixture": fixture_name, "leaf": index, "kind": f"leaf-{key}"}
                        )
            fixture_destination = official_destination / fixture_name
            fixture_destination.mkdir()
            shutil.copy2(trace_path, fixture_destination / trace_path.name)
            shutil.copy2(summary_path, fixture_destination / summary_path.name)
            aligned = trace_dir / "01-aligned.png"
            if aligned.is_file():
                shutil.copy2(aligned, fixture_destination / "01-aligned-actual.png")
        _write_jsonl(temporary / "official-v16-trace-failures.jsonl", official_failures)

        # Cross-version differences are explicit; v16 is not silently assumed
        # equivalent to v8 even when source crops match.
        by_key = {(str(value["version"]), str(value["fixture"])): value for value in summaries}
        version_diffs: list[dict[str, object]] = []
        for fixture_name in fixture_names:
            v8_recursion = _json(v8_root / fixture_name / "recursion.json")
            v16_recursion = _json(v16_root / fixture_name / "recursion.json")
            assert isinstance(v8_recursion, dict) and isinstance(v16_recursion, dict)
            v8_leaves = v8_recursion.get("leaves") or []
            v16_leaves = v16_recursion.get("leaves") or []
            changed_crops = []
            for index, (first, second) in enumerate(zip(v8_leaves, v16_leaves)):
                if first.get("source_bbox") != second.get("source_bbox") or first.get("source_crop_sha256") != second.get("source_crop_sha256"):
                    changed_crops.append(index)
            version_diffs.append(
                {
                    "fixture": fixture_name,
                    "v8_commit": v8_manifest.get("commit"),
                    "v16_commit": v16_manifest.get("commit"),
                    "v8_recursion_sha256": by_key[("v8", fixture_name)]["recursion_sha256"],
                    "v16_recursion_sha256": by_key[("v16", fixture_name)]["recursion_sha256"],
                    "same_leaf_count": len(v8_leaves) == len(v16_leaves),
                    "changed_actual_source_crop_indexes": changed_crops,
                    "same_sparse_shape": v8_recursion.get("sparse_shape") == v16_recursion.get("sparse_shape"),
                    "same_anchors": v8_recursion.get("anchors") == v16_recursion.get("anchors"),
                    "same_codes": v8_recursion.get("codes") == v16_recursion.get("codes"),
                    "same_x_tracks": v8_recursion.get("x_tracks") == v16_recursion.get("x_tracks"),
                }
            )
        _write_jsonl(temporary / "v8-v16-diff.jsonl", version_diffs)
        _write_jsonl(temporary / "baseline-summaries.jsonl", summaries)

        # High-signal actual large/multirow crop pages from v16.  A large crop
        # is recorded as context, not automatically classified as a defect.
        context_items: list[tuple[str, Path]] = []
        for fixture_name in fixture_names:
            path = temporary / "v16" / fixture_name / "leaf-ledger.jsonl"
            ledger = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ranked = sorted(
                ledger,
                key=lambda value: (
                    len(value["internal_blank_bands_at_least_8px"]),
                    int(value["height"]),
                    int(value["foreground_pixels"]),
                ),
                reverse=True,
            )[:32]
            for value in ranked:
                crop = temporary / value["actual_source_crop"]
                context_items.append(
                    (
                        f"{fixture_name[:18]} leaf={value['leaf']} h={value['height']} "
                        f"gaps={len(value['internal_blank_bands_at_least_8px'])} bbox={value['source_bbox']}",
                        crop,
                    )
                )
        context_items.sort(key=lambda value: value[0])
        if len(context_items) > 144:
            context_items = context_items[:144]
        context_sheet_root = temporary / "v16-context-crop-sheets"
        context_sheet_root.mkdir()
        context_sheets: list[str] = []
        for start in range(0, len(context_items), 24):
            stop = min(len(context_items), start + 24)
            path = context_sheet_root / f"context-{start:04d}-{stop - 1:04d}.png"
            _render_sheet(path, context_items, start=start, stop=stop)
            context_sheets.append(path.relative_to(temporary).as_posix())

        critical_failures = sum(
            int(value["actual_source_crop_failures"]) + int(value["structural_failures"])
            for value in summaries
        ) + len(official_failures)
        verdict = "REJECT" if critical_failures else "MANUAL_REVIEW_PENDING"
        (temporary / "verdict.txt").write_text(verdict + "\n", encoding="utf-8")

        with (temporary / "manual-audit.tsv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(("kind", "item", "manual_status", "notes"))
            for version in ("v8", "v16"):
                for fixture_name in fixture_names:
                    writer.writerow(
                        (
                            "legacy-navigation-sheet",
                            f"{version}/{fixture_name}/legacy-source-crops-sheet-navigation-only.png",
                            "PENDING",
                            "inspect actual crops; sheet is navigation only",
                        )
                    )
            for path in context_sheets:
                writer.writerow(
                    (
                        "v16-context-crop-sheet",
                        path,
                        "PENDING",
                        "large/multirow is context evidence, not an automatic error",
                    )
                )
            for value in summaries:
                for path in value["uncovered_foreground_evidence_sheets"]:
                    writer.writerow(
                        (
                            "uncovered-foreground-sheet",
                            path,
                            "PENDING",
                            "actual raw | independently isolated uncovered foreground; overlay not used",
                        )
                    )

        manifest = {
            "schema": "legacy-recursive-grid-external-oracle-audit-v1",
            "automated_verdict": verdict,
            "manual_verdict": "PENDING",
            "v8_root": str(v8_root.resolve()),
            "v16_root": str(v16_root.resolve()),
            "trace_root": str(trace_root.resolve()),
            "auditor_sha256": _sha(Path(__file__)),
            "primary_evidence": "actual source crop pixels + recursion/matrix/official JSON",
            "overlay_is_primary_evidence": False,
            "foreground_coverage_assumption": "max RGB distance >=24 from median 16px page border",
            "summaries": summaries,
            "official_trace_failures": len(official_failures),
            "context_sheet_count": len(context_sheets),
        }
        shutil.copy2(Path(__file__), temporary / "auditor-source.py")
        _write_json(temporary / "manifest.json", manifest)

        report = [
            "# Legacy recursive-grid external oracle audit",
            "",
            f"Automated verdict: **{verdict}**. Manual verdict: **PENDING**.",
            "",
            "The v16 traces are an external comparison oracle, not unquestioned ground truth. Actual source-crop integrity, foreground coverage, sparse anchors/codes/tracks, and official JSON agreement are checked independently. Overlays are supplemental only.",
            "",
            "| version | fixture | leaves | groups | sparse | crop failures | structural failures | coverage | overlap | multirow/context |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for value in summaries:
            coverage = value["coverage"]
            report.append(
                f"| {value['version']} | {value['fixture']} | {value['leaf_count']} | {value['group_count']} | "
                f"{value['sparse_shape'][0]}×{value['sparse_shape'][1]} | {value['actual_source_crop_failures']} | "
                f"{value['structural_failures']} | {coverage['coverage_rate']:.6f} | "
                f"{coverage['duplicated_foreground_rate']:.6f} | {value['multirow_context_leaf_count']} |"
            )
        report.extend(
            (
                "",
                "A structural automated clean result does not establish OCR quality. Fill `manual-audit.tsv`; contextual usability is assessed from actual crops, not their bbox size alone.",
            )
        )
        (temporary / "audit.md").write_text("\n".join(report) + "\n", encoding="utf-8")

        evidence = []
        for path in sorted(value for value in temporary.rglob("*") if value.is_file() and value.name != "manifest.json"):
            evidence.append(
                {
                    "path": path.relative_to(temporary).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha(path),
                }
            )
        _write_jsonl(temporary / "evidence-files.jsonl", evidence)
        temporary.rename(final)
        return final
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v8-root", type=Path, required=True)
    parser.add_argument("--v16-root", type=Path, required=True)
    parser.add_argument("--trace-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("debug/labs"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    print(
        audit(
            v8_root=args.v8_root,
            v16_root=args.v16_root,
            trace_root=args.trace_root,
            output_root=args.output_root,
            run_id=args.run_id,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
