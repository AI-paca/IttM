#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.formatting.structural_grammar import SparseMarkdownRow
from app.formatting.structural_journal import TemporaryStructuralJournal
from app.layout.pipeline import analyze_layout
from app.layout.contracts import LayoutStageSpec
from app.layout.recursive_grid import analyze_recursive_grid, recursive_grid_trace
from app.layout.stages import recursive_grid_config
from app.pipeline_config import resolve_pipeline_profile
from app.preprocessing import OcrPreprocessingPipeline
from app.services import convert_service


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _region_name(index: int, metadata: dict[str, Any]) -> str:
    row = metadata.get("grid_row")
    col = metadata.get("grid_col")
    if isinstance(row, int) and isinstance(col, int):
        return f"{index:03d}-r{row:03d}-c{col:02d}"
    return f"{index:03d}-{metadata.get('layout_kind', 'region')}"


def _layout_entry_for_region(
    region,
    reference,
    meta: dict[str, Any],
):
    metadata = region.metadata or {}
    if (
        metadata.get("layout_kind") == "recursive_grid_cell"
        and isinstance(metadata.get("grid_row"), int)
        and isinstance(metadata.get("grid_col"), int)
    ):
        sparse_codes = metadata.get("sparse_codes")
        content_bbox = metadata.get("content_bbox")
        content_left = (
            int(content_bbox[0])
            if (
                isinstance(content_bbox, tuple)
                and len(content_bbox) == 4
                and isinstance(content_bbox[0], int)
            )
            else None
        )
        return convert_service._LayoutJournalEntry(
            kind=1,
            reference=reference,
            bbox=region.bbox,
            anchor=(int(metadata["grid_row"]), int(metadata["grid_col"])),
            codes=(tuple(sparse_codes) if isinstance(sparse_codes, tuple) else ()),
            list_marker=bool(metadata.get("list_marker")),
            content_left=content_left,
            flags=tuple(meta.get("runtime_flags", ())),
        )
    return convert_service._LayoutJournalEntry(
        kind=0,
        reference=reference,
        bbox=region.bbox,
        flags=tuple(meta.get("runtime_flags", ())),
    )


def _render_structural_records(journal, entries) -> str:
    lines = []
    for entry in entries:
        lines.append(
            json.dumps(
                {
                    "kind": "sparse" if entry.kind == 1 else "plain",
                    "bbox": entry.bbox,
                    "anchor": entry.anchor,
                    "codes": entry.codes,
                    "list_marker": entry.list_marker,
                    "content_left": entry.content_left,
                    "flags": entry.flags,
                    "parts": list(journal.parts(entry.reference)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return "\n".join(lines)


def dump_trace(
    source: Path,
    output_dir: Path,
    *,
    engine_type: str,
    profile_name: str,
    layout_only: bool = False,
) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    regions_dir = output_dir / "regions"
    regions_dir.mkdir(parents=True, exist_ok=True)

    profile = resolve_pipeline_profile(engine_type, profile_name)

    with Image.open(source) as opened:
        original = opened.convert("RGB")
    pipeline = OcrPreprocessingPipeline.from_step_names(profile.image_preprocessing)
    image = pipeline.apply(original)
    if image is not original:
        original.close()
    image.save(output_dir / "01-aligned.png")

    recursive_stage = LayoutStageSpec(
        name="recursive_grid",
        parameters=profile.layout.default_parameters,
    )
    recursive_analysis = analyze_recursive_grid(
        image,
        recursive_grid_config(recursive_stage),
    )
    _write_text(
        output_dir / "02-recursive-grid-trace.json",
        json.dumps(
            _jsonable(recursive_grid_trace(recursive_analysis)),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    for leaf in recursive_analysis.leaves:
        leaf.image.close()

    if layout_only:
        _write_text(
            output_dir / "00-summary.json",
            json.dumps(
                {
                    "source": str(source),
                    "engine": engine_type,
                    "profile": profile.name,
                    "layout_only": True,
                    "recursive_profile": recursive_analysis.profile.kind,
                    "leaf_count": len(recursive_analysis.leaves),
                    "sparse_shape": [
                        recursive_analysis.signature.rows,
                        recursive_analysis.signature.cols,
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        image.close()
        return

    engine = convert_service._create_engine(engine_type, profile)

    regions, decision = analyze_layout(
        image,
        profile.layout,
        min_confirmed_cell_ratio=profile.grid_min_confirmed_cell_ratio,
    )
    layout_parameters = decision.stages[0].parameters if decision.stages else ()
    runtime_flags = set(convert_service._layout_runtime_flags(decision))
    totals = {
        "chunks": 0,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    region_records: list[dict[str, Any]] = []

    with TemporaryStructuralJournal() as journal:
        layout_entries = []
        for index, region in enumerate(regions, start=1):
            metadata = region.metadata or {}
            name = _region_name(index, metadata)
            region_path = regions_dir / f"{name}.png"
            region.image.save(region_path)
            parts, meta = convert_service._convert_layout_region(
                region,
                engine,
                profile,
                layout_parameters,
            )
            reference = journal.append(parts)
            layout_entries.append(_layout_entry_for_region(region, reference, meta))
            for key in totals:
                totals[key] += int(meta.get(key, 0))
            runtime_flags.update(meta.get("runtime_flags", ()))
            region_records.append(
                {
                    "index": index,
                    "name": name,
                    "kind": region.kind,
                    "bbox": region.bbox,
                    "image": str(region_path.relative_to(output_dir)),
                    "table": _jsonable(region.table),
                    "metadata": _jsonable(metadata),
                    "parts": parts,
                    "meta": _jsonable(meta),
                }
            )

        structural = convert_service._render_layout_journal(
            journal,
            layout_entries,
            structural_output=profile.structural_output,
            page_table_confirmed=(
                totals["tables_found"] > 0
                and not convert_service._looks_like_dark_ui_text_page(image)
            ),
        )
        page_parts = list(structural.parts)
        runtime_flags.update(structural.flags)
        grammar_input = _render_structural_records(journal, layout_entries)

    final_markdown = convert_service._finalize_markdown(
        "\n\n".join(page_parts),
        profile,
    )

    _write_text(output_dir / "02-regions.json", json.dumps(region_records, ensure_ascii=False, indent=2))
    _write_text(output_dir / "03-structural-records.jsonl", grammar_input)
    _write_text(output_dir / "04-grammar-output.md", "\n\n".join(page_parts).strip() + "\n")
    _write_text(output_dir / "05-final-markdown.md", final_markdown.strip() + "\n")
    _write_text(
        output_dir / "06-runtime-flags.txt",
        "\n".join(sorted(runtime_flags)) + "\n",
    )
    _write_text(
        output_dir / "00-summary.json",
        json.dumps(
            {
                "source": str(source),
                "engine": engine_type,
                "profile": profile.name,
                "decision": _jsonable(decision),
                "totals": totals,
                "region_count": len(regions),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    for region in regions:
        if region.image is not image:
            region.image.close()
    image.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump one OCR page through recursive-grid, OCR, and structural grammar stages.",
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--engine", default="tesseract", choices=("auto", "tesseract", "easyocr"))
    parser.add_argument("--profile", default="backend_tesseract_standard")
    parser.add_argument(
        "--layout-only",
        action="store_true",
        help="Stop after alignment, recursive segmentation, and sparse projection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dump_trace(
        args.source,
        args.output_dir,
        engine_type=args.engine,
        profile_name=args.profile,
        layout_only=args.layout_only,
    )
    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
