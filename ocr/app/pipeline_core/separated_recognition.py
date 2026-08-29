from __future__ import annotations

from collections.abc import Callable

from PIL import Image

from app.pipeline_config import OcrPipelineProfile
from app.pipeline_core.separated import (
    SeparatedOcrJob,
    SeparatedOcrWord,
    SeparatedRecognition,
)


TextFallback: type = Callable[..., str]


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
        else recognize_words(crop, psm=psm, min_conf=20)
        if callable(recognize_words) and job.languages == "rus+eng"
        else []
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
            (line_heights[len(line_heights) // 2] // 2)
            if line_heights
            else 4,
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
                    confidence_milli=round(confidence * 1_000),
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
