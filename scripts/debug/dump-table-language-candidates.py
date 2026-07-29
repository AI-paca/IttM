#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
OCR_ROOT = REPO_ROOT / "ocr"
if str(OCR_ROOT) not in sys.path:
    sys.path.insert(0, str(OCR_ROOT))

from app.chunking.vertical import table_words_to_rows
from app.engines.tesseract_engine import TesseractEngine
from app.layout.pipeline import analyze_layout
from app.pipeline_config import resolve_pipeline_profile
from app.preprocessing import OcrPreprocessingPipeline
from app.services import convert_service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="backend_tesseract_standard")
    args = parser.parse_args()

    profile = resolve_pipeline_profile("tesseract", args.profile)
    with Image.open(args.source) as opened:
        original = opened.convert("RGB")
    pipeline = OcrPreprocessingPipeline.from_step_names(profile.image_preprocessing)
    image = pipeline.apply(original)
    if image is not original:
        original.close()
    regions, _decision = analyze_layout(
        image,
        profile.layout,
        min_confirmed_cell_ratio=profile.grid_min_confirmed_cell_ratio,
    )
    engine = TesseractEngine(
        language_priority=profile.tesseract_language_priority,
        ocr_border_pixels=profile.ocr_border_pixels,
        edge_word_fallback_psms=profile.edge_word_fallback_psms,
        language_retry=profile.ocr_language_retry,
    )
    payload = []
    try:
        for region in regions:
            if region.kind != "table" or region.table is None:
                continue
            table = convert_service.logical_table_layout(region.image, region.table)
            table = convert_service.mark_table_empty_slots(region.image, table)
            languages = engine._single_language_candidates()
            rows_by_language = {}
            for language in languages:
                words = engine.recognize_words_for_language(
                    region.image,
                    language,
                    psm=11,
                    min_conf=5,
                )
                rows_by_language[language] = table_words_to_rows(table, words)
            payload.append(
                {
                    "bbox": region.bbox,
                    "rows": table.rows,
                    "columns": table.cols,
                    "candidates": rows_by_language,
                }
            )
    finally:
        for region in regions:
            if region.image is not image:
                region.image.close()
        image.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
