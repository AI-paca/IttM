from __future__ import annotations

import io
import os
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from app.pipeline_config import OcrPipelineProfile
from app.pipeline_core.separated import (
    SeparatedOcrJob,
    SeparatedOcrWord,
    SeparatedRecognition,
)
from app.sparse_pipeline.ocr_adapters import TesseractConfig, TesseractWorker
from app.sparse_pipeline.ocr_adapter_contracts import (
    OcrLanguageUnavailableError,
    OcrRecognitionMissError,
)

TextFallback: type = Callable[..., str]


def _recognize_stage_tesseract_words(
    crop: Image.Image,
    job: SeparatedOcrJob,
    *,
    psm: int,
) -> SeparatedRecognition | None:
    if psm not in {4, 6}:
        return None
    languages = tuple(value for value in job.languages.split("+") if value)
    if not languages:
        return None
    tessdata_value = os.environ.get("TESSDATA_PREFIX", "").strip()
    tessdata = Path(tessdata_value).resolve() if tessdata_value else None
    config = TesseractConfig(
        tessdata_directory=tessdata,
        languages=languages,
        psm=psm,
        # Gamma, dark-small-text normalization, and ordinary upscaling are
        # already part of the Rust job raster. The host owns only the OCR
        # engine's recognition-miss retry, which needs an OCR result first.
        upscale_min_height=0,
        dark_small_text_normalization=False,
        recognition_miss_retry_max_height=128,
        recognition_miss_retry_padding=16,
    )
    payload = io.BytesIO()
    crop.save(payload, format="PNG", compress_level=1, dpi=(300, 300))
    try:
        output = TesseractWorker(config).recognize(payload.getvalue())
    except (OcrLanguageUnavailableError, OcrRecognitionMissError):
        return SeparatedRecognition(text="", confidence_milli=0, words=())
    words = tuple(
        SeparatedOcrWord(
            text=word.text,
            bbox=(
                word.bbox.left,
                word.bbox.top,
                word.bbox.right,
                word.bbox.bottom,
            ),
            confidence_milli=round(word.confidence * 1_000_000),
        )
        for word in output.words
    )
    if not output.text.strip() or not words:
        return None
    return SeparatedRecognition(
        text=output.text,
        confidence_milli=0,
        words=words,
    )


def recognize_separated_block(
    crop: Image.Image,
    job: SeparatedOcrJob,
    engine: object,
    profile: OcrPipelineProfile,
    text_fallback: TextFallback,
) -> SeparatedRecognition:
    """Run one Rust-requested block through the persistent OCR language agenda."""

    psm = {
        0: profile.text_region_psm,
        1: profile.document_region_psm,
        2: profile.wide_text_region_psm,
    }.get(job.recognition_mode, profile.text_region_psm)
    language_engine = getattr(engine, "tesseract", engine)
    if hasattr(language_engine, "recognize_with_psm"):
        stage_result = _recognize_stage_tesseract_words(crop, job, psm=psm)
        if stage_result is not None:
            return stage_result
    recognize_for_language = getattr(
        language_engine,
        "recognize_words_for_language",
        None,
    )
    recognize_words = getattr(language_engine, "recognize_words", None)
    words = (
        recognize_for_language(
            crop,
            job.languages,
            psm=psm,
            min_conf=0,
        )
        if callable(recognize_for_language)
        else (
            recognize_words(crop, psm=psm, min_conf=20)
            if callable(recognize_words) and job.languages == "rus+eng"
            else []
        )
    )
    if words:
        # Tesseract already emits TSV words in block/paragraph/line/word order.
        # Sorting globally by pixel top corrupts one line whenever ascenders and
        # x-height words have slightly different top coordinates.
        ordered = tuple(words)
        line_heights = sorted(
            max(1, int(word["bbox"][3]) - int(word["bbox"][1]))
            for word in ordered
            if len(tuple(word.get("bbox", ()))) == 4
        )
        tolerance = max(
            4,
            (line_heights[len(line_heights) // 2] // 2) if line_heights else 4,
        )
        lines: list[list[str]] = []
        line_centers: list[int] = []
        evidence_words = []
        for word in ordered:
            bbox = tuple(word.get("bbox", ()))
            text = str(word.get("text", "")).strip()
            if len(bbox) != 4 or not text:
                continue
            left, top, right, bottom = (int(value) for value in bbox)
            if right <= left or bottom <= top:
                continue
            center = (top + bottom) // 2
            if not lines or abs(center - line_centers[-1]) > tolerance:
                lines.append([])
                line_centers.append(center)
            lines[-1].append(text)
            confidence = max(
                0.0,
                min(1.0, float(word.get("conf", 0.0)) / 100.0),
            )
            evidence_words.append(
                SeparatedOcrWord(
                    text=text,
                    bbox=(left, top, right, bottom),
                    confidence_milli=round(confidence * 1_000_000),
                )
            )
        text = "\n".join(" ".join(line) for line in lines if line)
        if text and evidence_words:
            return SeparatedRecognition(
                text=text,
                confidence_milli=0,
                words=tuple(evidence_words),
            )

    # A language-specific attempt without word evidence is unresolved.  The
    # Rust agenda, not a second adapter policy, decides the next language or
    # the bounded recursive split.
    return SeparatedRecognition(text="", confidence_milli=1)
