from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.sparse_pipeline.contact_sheets import (
    write_paired_contact_sheets,
    write_paired_contact_sheets_from_archive,
)
from app.sparse_pipeline.contracts import RecursiveNode, SparseCoordinateMode
from app.sparse_pipeline.geometry import GeometryBundle

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GeometryArtifactWriter:
    """Atomically publish all evidence needed to audit stage 1."""

    INDIVIDUAL_SEGMENT_CROP_LIMIT = 512

    def write(self, root: Path, *, run_id: str, bundle: GeometryBundle) -> Path:
        if type(run_id) is not str or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe characters")
        run_dir = root / run_id
        if run_dir.exists():
            raise FileExistsError(f"debug run already exists: {run_dir}")
        root.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=root))
        stage_dir = temporary_dir / "01-geometry"
        stage_dir.mkdir()
        try:
            self._write_bundle(stage_dir, bundle)
            try:
                temporary_dir.rename(run_dir)
            except OSError as exc:
                if run_dir.exists():
                    raise FileExistsError(
                        f"debug run already exists: {run_dir}"
                    ) from exc
                raise
            return run_dir
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def _write_bundle(self, stage_dir: Path, bundle: GeometryBundle) -> None:
        result = bundle.result
        aligned_sha256 = hashlib.sha256(
            memoryview(np.ascontiguousarray(bundle.aligned_rgb))
        ).hexdigest()
        if aligned_sha256 != result.aligned_rgb_sha256:
            raise ValueError(
                "geometry result was not derived from the supplied aligned RGB bundle"
            )
        segmentation = result.segmentation
        matrix = result.matrix
        manifest = {
            "semantic_stage": 1,
            "execution_step": 2,
            "stage_name": "geometry-sparse-matrix",
            "status": result.status.value,
            "source_sha256": hashlib.sha256(bundle.source_rgb.tobytes()).hexdigest(),
            "aligned_rgb_sha256": aligned_sha256,
            "source_size": list(result.alignment.transform.original_size),
            "aligned_size": list(result.alignment.transform.aligned_size),
            "foreground_pixels": result.alignment.foreground_pixels,
            "segments": len(segmentation.segments),
            "rules": len(segmentation.rules),
            "nodes": len(segmentation.nodes),
            "sparse_cells": len(matrix.cells),
            "limit_leaf_count": result.limit_leaf_count,
            "invariants": {
                "exact_ownership": True,
                "actual_segment_crops": True,
                "half_open_boxes": True,
                "source_immutable": True,
                "matrix_deterministic": True,
                "recursion_complete": result.limit_leaf_count == 0,
            },
        }
        legacy_trace = getattr(bundle, "v16_trace", None)
        excluded_foreground = getattr(
            bundle,
            "excluded_foreground_mask",
            None,
        )
        if legacy_trace is not None:
            if not isinstance(excluded_foreground, np.ndarray):
                raise ValueError(
                    "v16 geometry trace requires excluded foreground evidence"
                )
            manifest["legacy_projection"] = {
                "version": legacy_trace.version,
                "rows": legacy_trace.rows,
                "columns": legacy_trace.columns,
                "leaves": len(legacy_trace.leaves),
                "groups": len(legacy_trace.groups),
                "codes": len(legacy_trace.codes),
                "projection_sha256": legacy_trace.projection_sha256,
            }
            manifest["foreground_scope"] = {
                "definition": legacy_trace.foreground_definition,
                "all_detected_pixels": legacy_trace.foreground_pixels,
                "tracked_leaf_union_pixels": (
                    legacy_trace.tracked_foreground_pixels
                ),
                "excluded_outside_leaf_union_pixels": (
                    legacy_trace.excluded_foreground_pixels
                ),
                "excluded_mask_sha256": (
                    legacy_trace.excluded_foreground_sha256
                ),
            }
        self._write_json(stage_dir / "manifest.json", manifest)
        self._write_json(stage_dir / "alignment.json", asdict(result.alignment))
        self._write_jsonl(
            stage_dir / "nodes.jsonl", (asdict(node) for node in segmentation.nodes)
        )
        self._write_jsonl(
            stage_dir / "segments.jsonl",
            (asdict(segment) for segment in segmentation.segments),
        )
        self._write_jsonl(
            stage_dir / "rules.jsonl", (asdict(rule) for rule in segmentation.rules)
        )
        self._write_json(stage_dir / "matrix.json", self._matrix_json(matrix))

        self._save_rgb(stage_dir / "source.png", bundle.source_rgb)
        self._save_rgb(stage_dir / "aligned.png", bundle.aligned_rgb)
        self._save_mask(stage_dir / "foreground-mask.png", bundle.foreground_mask)
        self._save_mask(stage_dir / "rule-mask.png", bundle.rule_mask)
        self._save_rgb(stage_dir / "ownership.png", self._ownership_rgb(bundle))
        if legacy_trace is not None:
            assert isinstance(excluded_foreground, np.ndarray)
            self._write_legacy_projection(
                stage_dir,
                bundle=bundle,
                trace=legacy_trace,
                excluded_foreground=excluded_foreground,
            )
            self._save_rgb(
                stage_dir / "matrix-numbered-overlay.png",
                self._numbered_legacy_matrix_overlay(
                    bundle=bundle,
                    trace=legacy_trace,
                ),
            )
            self._write_text(
                stage_dir / "matrix-numbered-overlay.txt",
                "\n".join(
                    (
                        "M[row,column]=Snn: occupied sparse cell and segment",
                        "K=value: numeric structural code at that coordinate",
                        "magenta: logical x_track retained before serialization",
                        "cyan: contextual source leaf written as the segment bbox",
                        "Numbers are copied from matrix.json and legacy-matrix.json.",
                    )
                )
                + "\n",
            )
        self._save_rgb(
            stage_dir / "recursive-overlay.png", self._recursive_overlay(bundle)
        )
        self._save_rgb(
            stage_dir / "segments-overlay.png", self._segments_overlay(bundle)
        )
        self._save_rgb(stage_dir / "matrix-overlay.png", self._matrix_overlay(bundle))
        self._write_segment_crops(stage_dir, geometry=bundle)

        self._write_text(stage_dir / "tree.txt", self._tree_text(segmentation.nodes))
        self._write_text(stage_dir / "matrix.txt", self._matrix_text(bundle))
        self._write_text(stage_dir / "invariants.txt", self._invariant_text(bundle))

    @staticmethod
    def _ownership_rgb(bundle: GeometryBundle) -> np.ndarray:
        output = np.array(bundle.aligned_rgb, copy=True)
        output[bundle.rule_mask] = (225, 35, 35)
        palette = np.empty(
            (len(bundle.result.segmentation.segments), 3),
            dtype=np.uint8,
        )
        for index, _ in enumerate(bundle.result.segmentation.segments):
            digest = hashlib.sha256(f"segment-{index}".encode("ascii")).digest()
            palette[index] = np.asarray(
                (64 + digest[0] // 2, 64 + digest[1] // 2, 64 + digest[2] // 2),
                dtype=np.uint8,
            )
        owned = bundle.ownership >= 0
        output[owned] = palette[bundle.ownership[owned]]
        return output

    @staticmethod
    def _recursive_overlay(bundle: GeometryBundle) -> np.ndarray:
        image = Image.fromarray(np.array(bundle.aligned_rgb, copy=True), mode="RGB")
        draw = ImageDraw.Draw(image)
        for node in bundle.result.segmentation.nodes:
            color = (45, 90 + (node.depth * 41) % 150, 225)
            box = node.bbox
            draw.rectangle(
                (box.left, box.top, box.right - 1, box.bottom - 1),
                outline=color,
                width=1,
            )
            for separator in node.separator_boxes:
                draw.rectangle(
                    (
                        separator.left,
                        separator.top,
                        separator.right - 1,
                        separator.bottom - 1,
                    ),
                    fill=(255, 205, 40),
                )
            if node.split_coordinate is not None:
                if node.axis is not None and node.axis.value == "rows":
                    draw.line(
                        (
                            node.bbox.left,
                            node.split_coordinate,
                            node.bbox.right - 1,
                            node.split_coordinate,
                        ),
                        fill=(20, 210, 210),
                        width=1,
                    )
                else:
                    draw.line(
                        (
                            node.split_coordinate,
                            node.bbox.top,
                            node.split_coordinate,
                            node.bbox.bottom - 1,
                        ),
                        fill=(20, 210, 210),
                        width=1,
                    )
        return np.asarray(image, dtype=np.uint8)

    @staticmethod
    def _segments_overlay(bundle: GeometryBundle) -> np.ndarray:
        image = Image.fromarray(np.array(bundle.aligned_rgb, copy=True), mode="RGB")
        draw = ImageDraw.Draw(image)
        for segment in bundle.result.segmentation.segments:
            box = segment.bbox
            draw.rectangle(
                (box.left, box.top, box.right - 1, box.bottom - 1),
                outline=(20, 170, 80),
                width=1,
            )
        for rule in bundle.result.segmentation.rules:
            box = rule.bbox
            draw.rectangle(
                (box.left, box.top, box.right - 1, box.bottom - 1),
                outline=(220, 30, 30),
                width=1,
            )
        return np.asarray(image, dtype=np.uint8)

    @staticmethod
    def _matrix_overlay(bundle: GeometryBundle) -> np.ndarray:
        image = Image.fromarray(np.array(bundle.aligned_rgb, copy=True), mode="RGB")
        if (
            bundle.result.matrix.coordinate_mode
            is SparseCoordinateMode.LOGICAL_PROJECTION
        ):
            # Logical row/track ordinals have no pixel positions.  Drawing
            # them at y/x == ordinal fabricates a grid in the page corner.
            return np.asarray(image, dtype=np.uint8)
        draw = ImageDraw.Draw(image)
        width, height = bundle.result.alignment.transform.aligned_size
        for interval in bundle.result.matrix.rows:
            if interval.start:
                draw.line(
                    (0, interval.start, width - 1, interval.start),
                    fill=(165, 70, 210),
                    width=1,
                )
        for interval in bundle.result.matrix.columns:
            if interval.start:
                draw.line(
                    (interval.start, 0, interval.start, height - 1),
                    fill=(165, 70, 210),
                    width=1,
                )
        return np.asarray(image, dtype=np.uint8)

    @staticmethod
    def _numbered_legacy_matrix_overlay(
        *,
        bundle: GeometryBundle,
        trace: object,
    ) -> np.ndarray:
        """Draw literal logical cells and codes back on their source leaves.

        A v16 logical column is an x-track, not a pixel partition.  This
        overlay therefore uses the trace's retained physical x coordinate and
        each leaf's source bbox instead of inventing rectangular matrix cells.
        The result makes lossy projections visible: an occupied anchor and a
        structural code remain visually distinct even when they share a row.
        """

        header_height = 118
        source = np.array(bundle.aligned_rgb, copy=True)
        height, width = source.shape[:2]
        canvas = Image.new(
            "RGB",
            (width, height + header_height),
            (248, 248, 248),
        )
        canvas.paste(Image.fromarray(source, mode="RGB"), (0, header_height))
        draw = ImageDraw.Draw(canvas)
        font = _annotation_font(max(16, min(28, width // 150)))
        small_font = _annotation_font(max(14, min(22, width // 180)))

        matrix = bundle.result.matrix
        draw.text(
            (18, 10),
            "NUMERIC SPARSE MATRIX OVERLAY",
            fill=(20, 20, 20),
            font=font,
        )
        draw.text(
            (18, 43),
            (
                f"shape={len(matrix.rows)}x{len(matrix.columns)}  "
                f"M=occupied segment ({len(matrix.cells)})  "
                f"K=structural code ({len(matrix.structural_codes)})"
            ),
            fill=(20, 20, 20),
            font=small_font,
        )
        draw.text(
            (18, 72),
            (
                "blue M[row,col]=Snn: matrix payload;  "
                "orange K=value: numeric code;  magenta: logical x_track"
            ),
            fill=(20, 20, 20),
            font=small_font,
        )

        leaves_by_segment = {item.segment_id: item for item in trace.leaves}
        occupied_by_segment = {
            item.segment_id: (item.row, item.column) for item in matrix.cells
        }
        codes_by_segment: dict[str, list[tuple[int, int, int]]] = {}
        for item in matrix.structural_codes:
            codes_by_segment.setdefault(item.segment_id, []).append(
                (item.row, item.column, item.code)
            )

        if trace.leaves:
            track_top = min(item.source_bbox.top for item in trace.leaves)
            track_bottom = max(item.source_bbox.bottom for item in trace.leaves)
            for column, x_track in enumerate(trace.x_tracks):
                x = max(0, min(width - 1, int(x_track)))
                draw.line(
                    (
                        x,
                        header_height + track_top,
                        x,
                        header_height + track_bottom - 1,
                    ),
                    fill=(205, 35, 180),
                    width=2,
                )
                _draw_label(
                    draw,
                    x=x + 3,
                    y=header_height - 26,
                    text=f"col {column} @ x={x_track}",
                    font=small_font,
                    foreground=(255, 255, 255),
                    background=(145, 20, 125),
                    canvas_size=canvas.size,
                )

        for segment_index, segment in enumerate(
            bundle.result.segmentation.segments
        ):
            leaf = leaves_by_segment.get(segment.segment_id)
            if leaf is None:
                continue
            box = leaf.source_bbox
            draw.rectangle(
                (
                    box.left,
                    header_height + box.top,
                    box.right - 1,
                    header_height + box.bottom - 1,
                ),
                outline=(0, 165, 190),
                width=2,
            )
            occupied = occupied_by_segment.get(segment.segment_id)
            coordinates = set(
                codes_by_segment.get(segment.segment_id, ())
            )
            if occupied is not None:
                coordinates.add((occupied[0], occupied[1], -1))
            coordinate_pairs = sorted({(row, column) for row, column, _ in coordinates})
            for label_index, (row, column) in enumerate(coordinate_pairs):
                values = tuple(
                    code
                    for code_row, code_column, code in codes_by_segment.get(
                        segment.segment_id,
                        (),
                    )
                    if (code_row, code_column) == (row, column)
                )
                payload = occupied == (row, column)
                parts = []
                if payload:
                    parts.append(f"M[{row},{column}]=S{segment_index:02d}")
                parts.extend(f"K={value}" for value in values)
                x_track = (
                    trace.x_tracks[column]
                    if 0 <= column < len(trace.x_tracks)
                    else leaf.content_bbox.left
                )
                _draw_label(
                    draw,
                    x=int(x_track) + 5,
                    y=(
                        header_height
                        + box.top
                        + 4
                        + label_index * (_font_height(small_font) + 8)
                    ),
                    text=" ".join(parts),
                    font=small_font,
                    foreground=(255, 255, 255) if payload else (25, 20, 10),
                    background=(25, 75, 155) if payload else (245, 170, 35),
                    canvas_size=canvas.size,
                )
        return np.asarray(canvas, dtype=np.uint8)

    @staticmethod
    def _tree_text(nodes: tuple[RecursiveNode, ...]) -> str:
        values = []
        for node in nodes:
            indent = "  " * node.depth
            values.append(
                f"{indent}{node.node_id} bbox={node.bbox.as_tuple()} axis={getattr(node.axis, 'value', '-')} "
                f"boundary={node.split_coordinate if node.split_coordinate is not None else '-'} "
                f"segments={','.join(node.segment_ids) or '-'} stop={getattr(node.stop_reason, 'value', '-')}"
            )
        return "\n".join(values) + "\n"

    @staticmethod
    def _matrix_text(bundle: GeometryBundle) -> str:
        matrix = bundle.result.matrix
        values = [
            f"mode={matrix.coordinate_mode.value}",
            f"shape={len(matrix.rows)}x{len(matrix.columns)} nonzero={len(matrix.cells)}",
        ]
        values.extend(
            f"({cell.row},{cell.column})\t{cell.segment_id}" for cell in matrix.cells
        )
        values.extend(
            f"span\t{span.segment_id}\t[{span.row_start}:{span.row_stop},{span.column_start}:{span.column_stop}]"
            for span in matrix.spans
        )
        values.extend(
            f"code\t({item.row},{item.column})\t{item.segment_id}\t{item.code}"
            for item in matrix.structural_codes
        )
        return "\n".join(values) + "\n"

    @staticmethod
    def _invariant_text(bundle: GeometryBundle) -> str:
        result = bundle.result
        segment_pixels = sum(
            segment.ink_pixels for segment in result.segmentation.segments
        )
        rule_pixels = sum(
            rule.foreground_pixels for rule in result.segmentation.rules
        )
        return (
            "\n".join(
                (
                    f"foreground_pixels={result.alignment.foreground_pixels}",
                    f"segment_pixels={segment_pixels}",
                    f"rule_pixels={rule_pixels}",
                    "ownership_exact="
                    + str(
                        segment_pixels + rule_pixels
                        == result.alignment.foreground_pixels
                    ).lower(),
                    f"source_size={result.alignment.transform.original_size}",
                    f"aligned_size={result.alignment.transform.aligned_size}",
                    f"status={result.status.value}",
                    f"limit_leaves={result.limit_leaf_count}",
                    "matrix_coordinate_mode="
                    f"{result.matrix.coordinate_mode.value}",
                    "excluded_foreground_pixels="
                    f"{int(getattr(bundle, 'excluded_foreground_mask', np.zeros((), dtype=bool)).sum())}",
                )
            )
            + "\n"
        )

    @staticmethod
    def _matrix_json(matrix: object) -> dict[str, object]:
        value = asdict(matrix)
        mode = matrix.coordinate_mode
        value["coordinate_mode"] = mode.value
        if mode is SparseCoordinateMode.PIXEL_PARTITION:
            value.pop("coordinate_mode")
            value.pop("structural_codes")
            value.pop("projection_sha256")
        return value

    @classmethod
    def _write_legacy_projection(
        cls,
        stage: Path,
        *,
        bundle: GeometryBundle,
        trace: object,
        excluded_foreground: np.ndarray,
    ) -> None:
        cls._write_json(stage / "legacy-matrix.json", asdict(trace))
        lines = [
            f"version={trace.version}",
            f"shape={trace.rows}x{trace.columns}",
            "x_tracks=" + ",".join(str(item) for item in trace.x_tracks),
            f"projection_sha256={trace.projection_sha256}",
            f"groups={len(trace.groups)}",
            f"excluded_foreground_pixels={trace.excluded_foreground_pixels}",
            f"excluded_foreground_sha256={trace.excluded_foreground_sha256}",
        ]
        lines.extend(
            "group\t"
            f"{group.index}\t{group.bbox.as_tuple()}\t"
            + ",".join(group.segment_ids)
            for group in trace.groups
        )
        lines.extend(
            "leaf\t"
            f"{leaf.segment_id}\tbbox={leaf.source_bbox.as_tuple()}\t"
            f"content={leaf.content_bbox.as_tuple()}\tanchor={leaf.anchor}\t"
            f"codes={leaf.codes}\tdecisions="
            + ";".join(item.split for item in leaf.decisions)
            for leaf in trace.leaves
        )
        cls._write_text(stage / "legacy-matrix.txt", "\n".join(lines) + "\n")
        cls._save_mask(stage / "excluded-foreground.png", excluded_foreground)
        isolated = np.full(bundle.source_rgb.shape, 255, dtype=np.uint8)
        isolated[excluded_foreground] = bundle.source_rgb[excluded_foreground]
        cls._save_rgb(stage / "excluded-foreground-isolated.png", isolated)
        cls._write_json(
            stage / "legacy-ownership.json",
            {
                "ownership_raster": "ownership.png",
                "ownership_semantics": (
                    "one deterministic segment owner per tracked foreground "
                    "pixel; contextual bboxes may overlap"
                ),
                "segment_ids": [
                    item.segment_id
                    for item in bundle.result.segmentation.segments
                ],
                "tracked_foreground_pixels": trace.tracked_foreground_pixels,
                "excluded_foreground_mask": "excluded-foreground.png",
                "excluded_foreground_isolated": (
                    "excluded-foreground-isolated.png"
                ),
                "excluded_foreground_pixels": (
                    trace.excluded_foreground_pixels
                ),
                "excluded_foreground_sha256": (
                    trace.excluded_foreground_sha256
                ),
                "projection_sha256": trace.projection_sha256,
            },
        )

    @classmethod
    def _write_segment_crops(
        cls,
        stage: Path,
        *,
        geometry: GeometryBundle,
    ) -> None:
        """Persist actual bbox and exact-ownership pixels for every segment."""

        crop_root = stage / "segment-crops"
        segments = geometry.result.segmentation.segments
        compact = len(segments) > cls.INDIVIDUAL_SEGMENT_CROP_LIMIT
        archive_path = crop_root / "segments.zip"
        raw_root = crop_root / "raw"
        isolated_root = crop_root / "isolated"
        crop_root.mkdir(parents=True)
        if not compact:
            raw_root.mkdir()
            isolated_root.mkdir()

        matrix_cells: dict[str, list[list[int]]] = {
            item.segment_id: []
            for item in segments
        }
        for cell in geometry.result.matrix.cells:
            matrix_cells[cell.segment_id].append([cell.row, cell.column])
        matrix_spans = {
            item.segment_id: {
                "row_start": item.row_start,
                "row_stop": item.row_stop,
                "column_start": item.column_start,
                "column_stop": item.column_stop,
            }
            for item in geometry.result.matrix.spans
        }

        entries: list[dict[str, object]] = []
        file_contact_items: list[tuple[str, Path, Path]] = []
        archive_contact_items: list[tuple[str, str, str]] = []
        archive = ZipFile(archive_path, "w", compression=ZIP_STORED) if compact else None
        try:
            for index, segment in enumerate(segments):
                if not _SAFE_RUN_ID.fullmatch(segment.segment_id):
                    raise ValueError(
                        "segment_id contains unsafe artifact characters"
                    )
                box = segment.bbox
                raw = np.array(
                    geometry.aligned_rgb[
                        box.top : box.bottom,
                        box.left : box.right,
                    ],
                    copy=True,
                )
                ownership = geometry.ownership[
                    box.top : box.bottom,
                    box.left : box.right,
                ]
                foreground = geometry.foreground_mask[
                    box.top : box.bottom,
                    box.left : box.right,
                ]
                exact_mask = (ownership == index) & foreground
                ownership_pixels = int(np.count_nonzero(exact_mask))
                if ownership_pixels != segment.ink_pixels:
                    raise ValueError(
                        "isolated crop ownership disagrees with "
                        f"{segment.segment_id}"
                    )
                isolated = np.full(raw.shape, 255, dtype=np.uint8)
                isolated[exact_mask] = raw[exact_mask]

                raw_name = f"raw/{segment.segment_id}.png"
                isolated_name = f"isolated/{segment.segment_id}.png"
                if archive is not None:
                    raw_bytes = cls._rgb_png_bytes(raw)
                    isolated_bytes = cls._rgb_png_bytes(isolated)
                    archive.writestr(raw_name, raw_bytes)
                    archive.writestr(isolated_name, isolated_bytes)
                    raw_value: str | None = None
                    isolated_value: str | None = None
                    archive_contact_items.append(
                        (segment.segment_id, raw_name, isolated_name)
                    )
                    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                    isolated_sha256 = hashlib.sha256(isolated_bytes).hexdigest()
                else:
                    raw_path = crop_root / raw_name
                    isolated_path = crop_root / isolated_name
                    cls._save_rgb(raw_path, raw)
                    cls._save_rgb(isolated_path, isolated)
                    raw_value = raw_name
                    isolated_value = isolated_name
                    file_contact_items.append(
                        (segment.segment_id, raw_path, isolated_path)
                    )
                    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                    isolated_sha256 = hashlib.sha256(
                        isolated_path.read_bytes()
                    ).hexdigest()
                entries.append(
                    {
                    "segment_id": segment.segment_id,
                    "bbox": list(box.as_tuple()),
                    "width": box.width,
                    "height": box.height,
                    "kind": segment.kind.value,
                    "ink_pixels": segment.ink_pixels,
                    "ownership_pixels": ownership_pixels,
                    "row_index": segment.row_index,
                    "order_key": list(segment.order_key),
                    "parent_path": list(segment.parent_path),
                    "component_ids": list(segment.component_ids),
                    "sparse_cells": matrix_cells[segment.segment_id],
                    "sparse_span": matrix_spans[segment.segment_id],
                        "raw": raw_value,
                        "isolated": isolated_value,
                        "raw_archive_member": raw_name if compact else None,
                        "isolated_archive_member": (
                            isolated_name if compact else None
                        ),
                        "raw_sha256": raw_sha256,
                        "isolated_sha256": isolated_sha256,
                    }
                )
        finally:
            if archive is not None:
                archive.close()

        if compact:
            contact_sheets = write_paired_contact_sheets_from_archive(
                crop_root,
                archive_path=archive_path,
                stem="segments",
                first_label="raw bbox",
                second_label="isolated ownership",
                items=tuple(archive_contact_items),
            )
        else:
            contact_sheets = write_paired_contact_sheets(
                crop_root,
                stem="segments",
                first_label="raw bbox",
                second_label="isolated ownership",
                items=tuple(file_contact_items),
            )
        cls._write_json(
            crop_root / "manifest.json",
            {
                "schema": "sparse-segment-crops-v2",
                "storage": "archive" if compact else "files",
                "archive": archive_path.name if compact else None,
                "definition": {
                    "raw": (
                        "unaltered aligned-page pixels inside the segment "
                        "half-open bbox"
                    ),
                    "isolated": (
                        "white canvas with only foreground pixels exactly "
                        "owned by segment_id"
                    ),
                },
                "segments": len(entries),
                "contact_sheets": contact_sheets,
                "items": entries,
            },
        )
        cls._write_segment_gallery(
            crop_root / "gallery.md",
            entries=tuple(entries),
            contact_sheets=contact_sheets,
            compact=compact,
        )

    @staticmethod
    def _write_segment_gallery(
        path: Path,
        *,
        entries: tuple[dict[str, object], ...],
        contact_sheets: list[str],
        compact: bool,
    ) -> None:
        lines = [
            "# Actual Stage 1 segment crops",
            "",
            "`raw bbox` — точный crop aligned-page по bbox. `isolated "
            "ownership` — белый фон и только пиксели, реально принадлежащие "
            "этому segment_id.",
            "",
        ]
        if contact_sheets:
            lines.extend(("## Контактные листы", ""))
            lines.extend(f"![{item}]({item})" for item in contact_sheets)
        if compact:
            lines.extend(
                (
                    "",
                    "Полные raw/isolated PNG находятся в `segments.zip`; "
                    "имена и SHA-256 перечислены в `manifest.json`.",
                )
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
        for entry in entries:
            segment_id = str(entry["segment_id"])
            lines.extend(
                (
                    "",
                    f"## `{segment_id}`",
                    "",
                    f"bbox=`{entry['bbox']}`; "
                    f"size=`{entry['width']}×{entry['height']}`; "
                    f"ink/owned=`{entry['ink_pixels']}/"
                    f"{entry['ownership_pixels']}`; "
                    f"sparse cells=`{entry['sparse_cells']}`; "
                    f"span=`{entry['sparse_span']}`.",
                    "",
                    "| raw bbox crop | isolated ownership crop |",
                    "|---|---|",
                    f"| ![{segment_id} raw]({entry['raw']}) | "
                    f"![{segment_id} isolated]({entry['isolated']}) |",
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _save_rgb(path: Path, value: np.ndarray) -> None:
        Image.fromarray(value.astype(np.uint8), mode="RGB").save(path, format="PNG")

    @staticmethod
    def _rgb_png_bytes(value: np.ndarray) -> bytes:
        buffer = BytesIO()
        Image.fromarray(value.astype(np.uint8), mode="RGB").save(
            buffer,
            format="PNG",
            compress_level=1,
        )
        return buffer.getvalue()

    @staticmethod
    def _save_mask(path: Path, value: np.ndarray) -> None:
        Image.fromarray(value.astype(np.uint8) * 255, mode="L").save(path, format="PNG")

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, values: object) -> None:
        lines = [
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in values
        ]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")


def _annotation_font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _font_height(font: object) -> int:
    left, top, right, bottom = font.getbbox("Mg")
    return max(1, bottom - top)


def _draw_label(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    font: object,
    foreground: tuple[int, int, int],
    background: tuple[int, int, int],
    canvas_size: tuple[int, int],
) -> None:
    padding_x = 4
    padding_y = 2
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = right - left + 2 * padding_x
    height = bottom - top + 2 * padding_y
    canvas_width, canvas_height = canvas_size
    x = max(0, min(x, max(0, canvas_width - width)))
    y = max(0, min(y, max(0, canvas_height - height)))
    draw.rectangle((x, y, x + width, y + height), fill=background)
    draw.text(
        (x + padding_x - left, y + padding_y - top),
        text,
        fill=foreground,
        font=font,
    )
