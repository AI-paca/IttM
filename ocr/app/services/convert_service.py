import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator, Tuple

from PIL import Image, ImageOps

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    analyze_document_layout,
    erase_table_lines_for_ocr,
    LayoutRegion,
    logical_table_layout,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_rows,
    table_rows_to_markdown,
    table_words_to_rows,
)
from app.engines.auto_engine import AutoEngine
from app.formatting.markdown_formatter import MarkdownFormatter
from app.layout.contracts import FeatureValue
from app.layout.pipeline import analyze_layout
from app.layout.table_formatters import format_table_words
from app.pipeline_config import OcrPipelineProfile, resolve_pipeline_profile
from app.pipeline_flags import profile_flags
from app.preprocessing import OcrPreprocessingPipeline

DEFAULT_MAX_DECODED_IMAGE_PIXELS = 80_000_000
DEFAULT_MAX_PDF_RENDER_DIMENSION = 6000
DEFAULT_MAX_PDF_PAGES = 100
PDF_TEXT_LAYER_MIN_CHARS = 200
PDF_TEXT_LAYER_MIN_WORDS = 20
PDF_TEXT_LAYER_MIN_PAGE_RATIO = 0.5
PDF_MODES = frozenset({"auto", "raster"})
LONG_SCREENSHOT_MIN_HEIGHT = 6000
LONG_SCREENSHOT_MIN_ASPECT_RATIO = 8.0
DENSE_GRID_MIN_WIDTH = 1800
DENSE_GRID_MIN_HEIGHT = 1200
DENSE_GRID_MIN_HORIZONTAL_LINES = 8
DENSE_GRID_MIN_VERTICAL_LINES = 8


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def normalize_pdf_mode(value: str | None) -> str:
    mode = (value or "auto").strip().casefold()
    if mode not in PDF_MODES:
        known = ", ".join(sorted(PDF_MODES))
        raise ValueError(f"Unknown PDF mode '{value}'. Known modes: {known}")
    return mode


def _validate_decoded_image_size(image: Image.Image) -> None:
    width, height = image.size
    pixel_count = width * height
    limit = _positive_int_env(
        "OCR_MAX_DECODED_IMAGE_PIXELS",
        DEFAULT_MAX_DECODED_IMAGE_PIXELS,
    )
    if pixel_count > limit:
        raise ValueError(
            f"Decoded image contains {pixel_count} pixels; limit is {limit}"
        )


def _pdf_render_options(page_info: dict) -> dict:
    max_dimension = _positive_int_env(
        "OCR_MAX_PDF_RENDER_DIMENSION",
        DEFAULT_MAX_PDF_RENDER_DIMENSION,
    )
    page_size = next(
        (
            str(value)
            for key, value in page_info.items()
            if str(key).lower().endswith("size") and "pts" in str(value).lower()
        ),
        "",
    )
    match = re.search(r"([\d.]+)\s+x\s+([\d.]+)\s+pts", page_size, re.I)
    if not match:
        return {"dpi": 300}

    max_points = max(float(match.group(1)), float(match.group(2)))
    projected_dimension = max_points * 300 / 72
    if projected_dimension <= max_dimension:
        return {"dpi": 300}

    dpi = max(10, int(300 * max_dimension / projected_dimension))
    options = {"dpi": dpi}
    if max_points * dpi / 72 > max_dimension:
        options["size"] = max_dimension
    return options


def _pdf_text_page_is_usable(text: str) -> bool:
    compact = " ".join(text.split())
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", compact)
    return (
        len(compact) >= PDF_TEXT_LAYER_MIN_CHARS
        and len(words) >= PDF_TEXT_LAYER_MIN_WORDS
    )


def _usable_pdf_text_pages(pages: list[str]) -> list[str]:
    if not pages:
        return []

    usable_count = sum(1 for page in pages if _pdf_text_page_is_usable(page))
    if usable_count / len(pages) < PDF_TEXT_LAYER_MIN_PAGE_RATIO:
        return []

    return [page.replace("\f", "").rstrip() for page in pages]


def _extract_pdf_text_layer_pages(content: bytes, filename: str) -> list[str]:
    if Path(filename).suffix.lower() != ".pdf" or shutil.which("pdftotext") is None:
        return []

    try:
        from pdf2image import pdfinfo_from_path

        with tempfile.TemporaryDirectory(prefix="ittm-pdf-text-") as temp_dir:
            pdf_path = Path(temp_dir) / "document.pdf"
            pdf_path.write_bytes(content)
            page_count = int(pdfinfo_from_path(str(pdf_path)).get("Pages", 0))
            if page_count <= 0:
                return []
            page_limit = _positive_int_env(
                "OCR_MAX_PDF_PAGES",
                DEFAULT_MAX_PDF_PAGES,
            )
            if page_count > page_limit:
                return []

            pages = []
            for page_number in range(1, page_count + 1):
                completed = subprocess.run(
                    [
                        "pdftotext",
                        "-layout",
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        str(pdf_path),
                        "-",
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if completed.returncode != 0:
                    return []
                pages.append(completed.stdout)
            return _usable_pdf_text_pages(pages)
    except Exception:
        return []


def _prepared_image(
    image: Image.Image, image_pipeline: OcrPreprocessingPipeline
) -> Image.Image:
    oriented = ImageOps.exif_transpose(image)
    _validate_decoded_image_size(oriented)
    oriented.load()
    if oriented.mode in ("RGBA", "LA") or (
        oriented.mode == "P" and "transparency" in oriented.info
    ):
        rgba_image = oriented.convert("RGBA")
        try:
            base_image = Image.new("RGB", rgba_image.size, (255, 255, 255))
            base_image.paste(rgba_image, mask=rgba_image.split()[3])
        finally:
            rgba_image.close()
    else:
        base_image = oriented.convert("RGB")
    if oriented is not image:
        oriented.close()

    processed = image_pipeline.apply(base_image)
    if processed is not base_image:
        base_image.close()
    return processed


def _iter_document_images(
    content: bytes,
    filename: str,
    image_pipeline: OcrPreprocessingPipeline,
) -> Iterator[Image.Image]:
    if Path(filename).suffix.lower() != ".pdf":
        try:
            with Image.open(BytesIO(content)) as image:
                yield _prepared_image(image, image_pipeline)
        except Exception as exc:
            raise ValueError(f"Could not load image: {str(exc)}") from exc
        return

    try:
        from pdf2image import convert_from_path, pdfinfo_from_path

        with tempfile.TemporaryDirectory(prefix="ittm-pdf-") as temp_dir:
            pdf_path = Path(temp_dir) / "document.pdf"
            pdf_path.write_bytes(content)
            page_count = int(pdfinfo_from_path(str(pdf_path)).get("Pages", 0))
            if page_count <= 0:
                raise ValueError("PDF contains no pages")
            page_limit = _positive_int_env(
                "OCR_MAX_PDF_PAGES",
                DEFAULT_MAX_PDF_PAGES,
            )
            if page_count > page_limit:
                raise ValueError(
                    f"PDF contains {page_count} pages; limit is {page_limit}"
                )

            for page_number in range(1, page_count + 1):
                print(f"[PDF] Rendering page {page_number}/{page_count}", flush=True)
                page_info = pdfinfo_from_path(
                    str(pdf_path),
                    first_page=page_number,
                    last_page=page_number,
                )
                pages = convert_from_path(
                    str(pdf_path),
                    fmt="png",
                    first_page=page_number,
                    last_page=page_number,
                    thread_count=1,
                    **_pdf_render_options(page_info),
                )
                if not pages:
                    raise ValueError(f"PDF page {page_number} could not be rendered")
                page = pages[0]
                try:
                    _validate_decoded_image_size(page)
                    yield page.convert("RGB")
                finally:
                    for rendered_page in pages:
                        rendered_page.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to process PDF: {str(exc)}") from exc


MAX_DIRECT_TABLE_HEIGHT = 3600
MIN_SEGMENTED_TABLE_HEIGHT = 4000
MIN_SEGMENTED_TABLE_ASPECT_RATIO = 4.0
MIN_SEGMENTED_TABLE_CELLS = 500


def _format_card_to_markdown(card_text: str, card_index: int) -> str:
    """
    Formats a single chunk OCR result to Markdown.
    """
    return card_text.strip()


def _text_psm_for_image_region(
    image: Image.Image,
    profile: OcrPipelineProfile | None = None,
) -> int:
    if _is_dewarped_projector_slide(image):
        return profile.document_region_psm if profile else 3
    width, height = image.size
    aspect = height / max(1, width)
    if 1300 <= width <= 1900 and 1800 <= height <= 2600 and 1.2 <= aspect <= 1.7:
        return profile.document_region_psm if profile else 3
    if width >= 2200 and height >= 1500 and aspect <= 0.9:
        if _ink_ratio(image) >= 0.05:
            return profile.wide_text_region_psm if profile else 11
        return profile.document_region_psm if profile else 3
    if width >= 1600 and height >= 1000:
        return profile.document_region_psm if profile else 3
    return profile.text_region_psm if profile else 6


def _ink_ratio(image: Image.Image) -> float:
    gray = image.convert("L")
    try:
        histogram = gray.histogram()
        total = sum(histogram)
        if total <= 0:
            return 0.0
        return sum(histogram[:220]) / total
    finally:
        if gray is not image:
            gray.close()


def _looks_like_edge_to_edge_word(image: Image.Image) -> bool:
    width, height = image.size
    if width < 1200 or height < 600:
        return False
    aspect = height / max(1, width)
    if not 0.25 <= aspect <= 1.0:
        return False

    edge = max(2, min(12, min(width, height) // 80))
    strips = [
        image.crop((0, 0, width, edge)),
        image.crop((0, height - edge, width, height)),
        image.crop((0, 0, edge, height)),
        image.crop((width - edge, 0, width, height)),
    ]
    try:
        top, bottom, left, right = (_ink_ratio(strip) for strip in strips)
        overall = _ink_ratio(image)
    finally:
        for strip in strips:
            strip.close()

    return (
        0.02 <= top <= 0.20
        and 0.02 <= bottom <= 0.20
        and left >= 0.10
        and right >= 0.10
        and overall <= 0.80
    )


def _is_dewarped_projector_slide(image: Image.Image) -> bool:
    width, height = image.size
    aspect = height / max(1, width)
    return 1800 <= width <= 2200 and 1000 <= height <= 1400 and 0.5 <= aspect <= 0.75


def _ocr_token_count(text: str) -> int:
    return len(re.findall(r"[\w]+", text, re.UNICODE))


def _projection_line_count(mask, *, axis: int, minimum: int) -> int:
    import numpy as np

    projection = np.count_nonzero(mask > 0, axis=axis)
    indexes = np.flatnonzero(projection >= minimum)
    if not indexes.size:
        return 0

    count = 1
    previous = int(indexes[0])
    for raw_index in indexes[1:]:
        index = int(raw_index)
        if index - previous > 3:
            count += 1
        previous = index
    return count


def _looks_like_dense_grid_page(image: Image.Image) -> bool:
    width, height = image.size
    if width < DENSE_GRID_MIN_WIDTH or height < DENSE_GRID_MIN_HEIGHT:
        return False
    if height / max(1, width) > 1.6:
        return False

    try:
        import cv2
        import numpy as np
    except Exception:
        return False

    gray_image = image.convert("L")
    try:
        gray = np.array(gray_image)
    finally:
        if gray_image is not image:
            gray_image.close()
    if width * height > 8_000_000:
        scale = (8_000_000 / (width * height)) ** 0.5
        gray = cv2.resize(
            gray,
            (
                max(1, int(round(width * scale))),
                max(1, int(round(height * scale))),
            ),
            interpolation=cv2.INTER_AREA,
        )

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(
        blurred,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    analysis_height, analysis_width = binary.shape[:2]
    horizontal = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (max(30, analysis_width // 24), 1),
        ),
    )
    vertical = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (1, max(24, analysis_height // 24)),
        ),
    )
    horizontal_lines = _projection_line_count(
        horizontal,
        axis=1,
        minimum=max(40, int(analysis_width * 0.35)),
    )
    vertical_lines = _projection_line_count(
        vertical,
        axis=0,
        minimum=max(40, int(analysis_height * 0.35)),
    )
    foreground_ratio = float(np.mean(binary > 0))
    return horizontal_lines >= DENSE_GRID_MIN_HORIZONTAL_LINES and (
        vertical_lines >= DENSE_GRID_MIN_VERTICAL_LINES
        or foreground_ratio <= 0.08
        or horizontal_lines >= 30
    )


def _looks_like_sparse_cover_page(image: Image.Image) -> bool:
    width, height = image.size
    return (
        width >= 2200
        and height >= 1500
        and height / max(1, width) <= 0.9
        and _ink_ratio(image) <= 0.05
    )


def _overlapping_starts(limit: int, size: int, overlap: int) -> list[int]:
    size = max(1, min(limit, size))
    overlap = max(0, min(size - 1, overlap))
    if limit <= size:
        return [0]

    starts = list(range(0, limit - size + 1, size - overlap))
    last = limit - size
    if starts[-1] != last:
        starts.append(last)
    return starts


def _recognize_dense_grid_crop(
    engine,
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    *,
    scale: int,
    psm: int,
) -> str:
    crop = image.crop(bbox)
    resample = getattr(Image, "Resampling", Image).LANCZOS
    enlarged = crop.resize(
        (max(1, crop.width * scale), max(1, crop.height * scale)),
        resample,
    )
    prepared = erase_table_lines_for_ocr(enlarged)
    try:
        return engine.recognize(
            prepared,
            mode="text_mode",
            psm=psm,
        )
    finally:
        if prepared is not enlarged:
            prepared.close()
        enlarged.close()
        crop.close()


def _recognize_dense_grid_tiles(
    engine,
    image: Image.Image,
    *,
    columns: int,
    rows: int,
    scale: int,
    psm: int,
) -> tuple[list[str], int]:
    width, height = image.size
    tile_width = max(1, (width + columns - 1) // columns)
    tile_height = max(1, (height + rows - 1) // rows)
    overlap_x = max(24, tile_width // 12)
    overlap_y = max(24, tile_height // 12)
    texts = []
    calls = 0
    for top in _overlapping_starts(height, tile_height, overlap_y):
        for left in _overlapping_starts(width, tile_width, overlap_x):
            text = _recognize_dense_grid_crop(
                engine,
                image,
                (
                    left,
                    top,
                    min(width, left + tile_width),
                    min(height, top + tile_height),
                ),
                scale=scale,
                psm=psm,
            )
            calls += 1
            if text.strip():
                texts.append(text)
    return texts, calls


def _recognize_dense_grid_bands(
    engine,
    image: Image.Image,
    *,
    left_ratio: float,
    right_ratio: float,
    band_ratio: float,
    overlap_ratio: float,
    scale: int,
    psm: int,
) -> tuple[list[str], int]:
    width, height = image.size
    left = max(0, min(width - 1, int(round(width * left_ratio))))
    right = max(left + 1, min(width, int(round(width * right_ratio))))
    band_height = max(24, min(height, int(round(width * band_ratio))))
    overlap = max(0, min(band_height - 1, int(round(width * overlap_ratio))))
    texts = []
    calls = 0
    for top in _overlapping_starts(height, band_height, overlap):
        text = _recognize_dense_grid_crop(
            engine,
            image,
            (left, top, right, min(height, top + band_height)),
            scale=scale,
            psm=psm,
        )
        calls += 1
        if text.strip():
            texts.append(text)
    return texts, calls


def _recognize_dense_grid_page(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> tuple[str, int]:
    recognition_engine = _extra_pass_engine(engine, profile)
    width, height = image.size
    target_width = max(width, profile.dense_grid_target_width)
    if target_width != width:
        target_height = max(1, int(round(height * target_width / width)))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        working = image.resize((target_width, target_height), resample)
    else:
        working = image

    texts = []
    calls = 0
    try:
        for columns, rows, scale, psm in (
            (6, 5, 3, profile.wide_text_region_psm),
            (5, 4, 2, profile.text_region_psm),
        ):
            pass_texts, pass_calls = _recognize_dense_grid_tiles(
                recognition_engine,
                working,
                columns=columns,
                rows=rows,
                scale=scale,
                psm=psm,
            )
            texts.extend(pass_texts)
            calls += pass_calls

        band_passes = (
            (0.0, 0.38, 360 / 3300, 60 / 3300, 4, profile.text_region_psm),
            (0.0, 0.38, 360 / 3300, 60 / 3300, 4, profile.wide_text_region_psm),
            (85 / 3300, 390 / 3300, 360 / 3300, 60 / 3300, 5, profile.text_region_psm),
            (
                85 / 3300,
                390 / 3300,
                360 / 3300,
                60 / 3300,
                5,
                profile.wide_text_region_psm,
            ),
            (85 / 3300, 180 / 3300, 360 / 3300, 60 / 3300, 6, profile.text_region_psm),
            (130 / 3300, 390 / 3300, 100 / 3300, 20 / 3300, 6, profile.text_region_psm),
        )
        for left, right, band, overlap, scale, psm in band_passes:
            pass_texts, pass_calls = _recognize_dense_grid_bands(
                recognition_engine,
                working,
                left_ratio=left,
                right_ratio=right,
                band_ratio=band,
                overlap_ratio=overlap,
                scale=scale,
                psm=psm,
            )
            texts.extend(pass_texts)
            calls += pass_calls

        header_height = max(1, int(round(working.width * 220 / 3300)))
        header_passes = (
            (0.0, 1200 / 3300, 4, profile.text_region_psm),
            (0.0, 1200 / 3300, 4, profile.wide_text_region_psm),
            (0.0, 2200 / 3300, 3, profile.text_region_psm),
            (0.0, 2200 / 3300, 3, profile.wide_text_region_psm),
            (2600 / 3300, 1.0, 4, profile.text_region_psm),
            (2600 / 3300, 1.0, 4, profile.wide_text_region_psm),
        )
        for left_ratio, right_ratio, scale, psm in header_passes:
            left = int(round(working.width * left_ratio))
            right = int(round(working.width * right_ratio))
            text = _recognize_dense_grid_crop(
                recognition_engine,
                working,
                (left, 0, max(left + 1, right), header_height),
                scale=scale,
                psm=psm,
            )
            calls += 1
            if text.strip():
                texts.append(text)
    finally:
        if working is not image:
            working.close()

    return "\n\n".join(dedupe_chunks(texts)), calls


def _recognize_sparse_cover_page(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> tuple[str, int]:
    recognition_engine = _extra_pass_engine(
        engine,
        profile,
        language_priority=("rus", "eng"),
        ocr_border_pixels=0,
    )
    texts = []
    calls = 0
    for psm in (profile.wide_text_region_psm, 12):
        text = recognition_engine.recognize(image, mode="text_mode", psm=psm)
        calls += 1
        if text.strip():
            texts.append(text)

    top_height = min(image.height, max(1, int(round(image.width * 0.15))))
    top_right = min(image.width, max(1, int(round(image.width * 0.55))))
    for psm in (profile.document_region_psm, profile.text_region_psm):
        text = _recognize_dense_grid_crop(
            recognition_engine,
            image,
            (0, 0, top_right, top_height),
            scale=2,
            psm=psm,
        )
        calls += 1
        if text.strip():
            texts.append(text)
    return "\n\n".join(dedupe_chunks(texts)), calls


def _recognize_projector_slide_fallback(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> str:
    recognition_engine = _extra_pass_engine(
        engine,
        profile,
        language_priority=("eng", "rus"),
        ocr_border_pixels=0,
    )
    return recognition_engine.recognize(
        image,
        mode="text_mode",
        psm=profile.document_region_psm,
    )


def _engine_name(engine) -> str:
    try:
        info = engine.info()
    except Exception:
        return ""
    return str(info.get("engine", ""))


def _create_sparse_text_fallback_engine(profile: OcrPipelineProfile):
    if profile.sparse_text_fallback_engine != "tesseract":
        return None
    from app.engines.tesseract_engine import TesseractEngine

    return TesseractEngine(
        language_priority=profile.tesseract_language_priority,
        ocr_border_pixels=profile.ocr_border_pixels,
        edge_word_fallback_psms=profile.edge_word_fallback_psms,
    )


def _extra_pass_engine(
    engine,
    profile: OcrPipelineProfile,
    *,
    language_priority: tuple[str, ...] | None = None,
    ocr_border_pixels: int | None = None,
):
    if not profile.sparse_text_fallback_engine:
        return engine
    if _engine_name(engine) == profile.sparse_text_fallback_engine:
        return engine
    if profile.sparse_text_fallback_engine != "tesseract":
        return engine

    from app.engines.tesseract_engine import TesseractEngine

    return TesseractEngine(
        language_priority=language_priority or profile.tesseract_language_priority,
        ocr_border_pixels=(profile.ocr_border_pixels if ocr_border_pixels is None else ocr_border_pixels),
        edge_word_fallback_psms=profile.edge_word_fallback_psms,
    )


def _engine_chain(engine, profile: OcrPipelineProfile) -> list[str]:
    primary = _engine_name(engine) or "unknown"
    chain = [primary]
    fallback = profile.sparse_text_fallback_engine
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def _recognize_text_with_sparse_fallback(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
    *,
    mode: str = "text_mode",
    psm: int = 6,
    min_fallback_tokens: int | None = None,
) -> str:
    primary_text = engine.recognize(image, mode=mode, psm=psm)
    if (
        not profile.sparse_text_fallback_engine
        or _engine_name(engine) == profile.sparse_text_fallback_engine
    ):
        return primary_text

    primary_tokens = _ocr_token_count(primary_text)
    fallback_engine = _create_sparse_text_fallback_engine(profile)
    if fallback_engine is None:
        return primary_text

    fallback_text = fallback_engine.recognize(image, mode=mode, psm=psm)
    fallback_tokens = _ocr_token_count(fallback_text)
    minimum_tokens = (
        profile.sparse_text_fallback_min_tokens
        if min_fallback_tokens is None
        else min_fallback_tokens
    )
    if fallback_tokens < minimum_tokens:
        return primary_text
    if fallback_tokens < max(
        minimum_tokens,
        int(primary_tokens * profile.sparse_text_fallback_min_ratio),
    ):
        return primary_text
    if not primary_text.strip():
        return fallback_text
    return "\n\n".join(dedupe_chunks([primary_text, fallback_text]))


def _recognize_image_region(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> Tuple[list[str], int, int]:
    width, height = image.size
    text_psm = _text_psm_for_image_region(image, profile)
    min_fallback_tokens = (
        profile.edge_word_fallback_min_tokens
        if _looks_like_edge_to_edge_word(image)
        else None
    )
    if height <= 1600 or (width > 0 and height / width <= 1.8):
        return (
            [
                _recognize_text_with_sparse_fallback(
                    engine,
                    image,
                    profile,
                    mode="text_mode",
                    psm=text_psm,
                    min_fallback_tokens=min_fallback_tokens,
                )
            ],
            1,
            0,
        )

    cards = split_by_blank_bands(image, min_chunk_height=200)
    if cards and len(cards) > 1:
        page_parts = []
        cards_found = 0
        for i, card_img in enumerate(cards):
            try:
                cards_found += 1
                card_text = _recognize_text_with_sparse_fallback(
                    engine,
                    card_img,
                    profile,
                    mode="text_mode",
                    psm=_text_psm_for_image_region(card_img, profile),
                )
                page_parts.append(_format_card_to_markdown(card_text, i))
            finally:
                card_img.close()
        return page_parts, cards_found, cards_found

    chunks = split_vertical(image, chunk_height=1200, overlap=100)
    page_texts = []
    for chunk in chunks:
        try:
            page_texts.append(
                _recognize_text_with_sparse_fallback(
                    engine,
                    chunk,
                    profile,
                    mode="text_mode",
                    psm=_text_psm_for_image_region(chunk, profile),
                )
            )
        finally:
            if chunk is not image:
                chunk.close()
    return ["\n\n".join(dedupe_chunks(page_texts))], len(chunks), 0


def _should_segment_table_region(image: Image.Image, table) -> bool:
    width, height = image.size
    return (
        height >= MIN_SEGMENTED_TABLE_HEIGHT
        and height / max(1, width) >= MIN_SEGMENTED_TABLE_ASPECT_RATIO
        and len(table.cells) > MIN_SEGMENTED_TABLE_CELLS
    )


def _recognize_table_cell(engine, image: Image.Image) -> str:
    width, height = image.size
    psm = 7 if height <= 80 or width <= 180 else 6
    return engine.recognize(image, mode="text_mode", psm=psm)


def _line_bounded_segments(
    lines: tuple[int, ...], limit: int, max_span: int
) -> list[tuple[int, int]]:
    if len(lines) < 2:
        return [(0, limit)]

    normalized = sorted({max(0, min(limit, line)) for line in lines})
    if normalized[0] > 0:
        normalized.insert(0, 0)
    if normalized[-1] < limit:
        normalized.append(limit)

    segments = []
    start_index = 0
    while start_index < len(normalized) - 1:
        end_index = start_index + 1
        while (
            end_index + 1 < len(normalized)
            and normalized[end_index + 1] - normalized[start_index] <= max_span
        ):
            end_index += 1

        if normalized[end_index] <= normalized[start_index]:
            end_index = start_index + 1

        segments.append((normalized[start_index], normalized[end_index]))
        start_index = end_index

    return segments


def _recognize_scaled_words(
    engine, image: Image.Image, *, scale: int = 3, psm: int = 6, min_conf: int = 0
) -> list[dict]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return []

    resample = getattr(Image, "Resampling", Image).LANCZOS
    scaled = image.resize(
        (max(1, image.size[0] * scale), max(1, image.size[1] * scale)), resample
    )
    prepared = erase_table_lines_for_ocr(scaled)
    try:
        words = recognize_words(prepared, psm=psm, min_conf=min_conf)
    finally:
        if prepared is not scaled:
            prepared.close()
        scaled.close()
    scaled_words = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        left, top, right, bottom = bbox
        scaled_words.append(
            {
                **word,
                "bbox": (
                    int(round(left / scale)),
                    int(round(top / scale)),
                    int(round(right / scale)),
                    int(round(bottom / scale)),
                ),
            }
        )
    return scaled_words


def _merge_table_left_strip_words(
    engine,
    image: Image.Image,
    table,
    words: list[dict],
    *,
    psm: int,
) -> tuple[list[dict], int]:
    if len(table.x_lines) < 3:
        return words, 0

    left_limit = min(image.size[0], max(1, int(table.x_lines[2])))
    if left_limit <= 1:
        return words, 0

    left_strip = image.crop((0, 0, left_limit, image.size[1]))
    try:
        left_words = _recognize_scaled_words(
            engine,
            left_strip,
            scale=3,
            psm=psm,
            min_conf=0,
        )
    finally:
        left_strip.close()
    if len(left_words) < 10:
        return words, 1

    remaining = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        left, _, right, _ = bbox
        if (left + right) / 2 > left_limit:
            remaining.append(word)

    return [*remaining, *left_words], 1


def _recognize_table_words(
    engine,
    image: Image.Image,
    table,
    profile: OcrPipelineProfile,
    *,
    strategy: str = "bounded_tiles",
) -> tuple[list[dict], int]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return [], 0

    width, height = image.size
    if strategy not in {
        "bounded_tiles",
        "single_pass_with_left_strip",
    }:
        raise ValueError(f"Unknown table word recognition strategy '{strategy}'")

    single_pass = (
        strategy == "single_pass_with_left_strip" and height <= MAX_DIRECT_TABLE_HEIGHT
    )
    if single_pass or (table.cols <= 4 and height <= 3600):
        prepared = erase_table_lines_for_ocr(image)
        try:
            words = recognize_words(prepared, psm=profile.table_word_psm, min_conf=18)
        finally:
            if prepared is not image:
                prepared.close()
        if single_pass:
            merged_words, extra_calls = _merge_table_left_strip_words(
                engine,
                image,
                table,
                words,
                psm=profile.table_word_psm,
            )
            return merged_words, 1 + extra_calls
        return words, 1

    x_segments = _line_bounded_segments(table.x_lines, width, max_span=1700)
    y_segments = _line_bounded_segments(table.y_lines, height, max_span=1300)
    psm = (
        profile.large_table_word_psm
        if len(table.cells) > 200
        else profile.table_word_psm
    )
    min_conf = 18 if len(table.cells) <= 200 else 25

    words = []
    word_calls = 0
    for y1, y2 in y_segments:
        for x1, x2 in x_segments:
            tile = image.crop((x1, y1, x2, y2))
            prepared = erase_table_lines_for_ocr(tile)
            try:
                tile_words = recognize_words(
                    prepared,
                    psm=psm,
                    min_conf=min_conf,
                )
                word_calls += 1
            finally:
                if prepared is not tile:
                    prepared.close()
                tile.close()
            for word in tile_words:
                bbox = word.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                bx1, by1, bx2, by2 = bbox
                words.append(
                    {
                        **word,
                        "bbox": (bx1 + x1, by1 + y1, bx2 + x1, by2 + y1),
                    }
                )

    return words, word_calls


def _table_word_cell_coverage(table, words: list[dict]) -> float:
    if not table.cells:
        return 0.0

    rows = table_words_to_rows(table, words)
    populated_cells = sum(bool(cell.strip()) for row in rows for cell in row)
    return populated_cells / len(table.cells)


def _table_row_cell_coverage(table, rows: list[list[str]]) -> float:
    if not table.cells:
        return 0.0
    populated_cells = sum(bool(cell.strip()) for row in rows for cell in row)
    return populated_cells / len(table.cells)


def _should_append_table_raw_text_fallback(
    profile: OcrPipelineProfile,
    table,
) -> bool:
    if not profile.table_raw_text_fallback:
        return False
    return (
        table.rows >= profile.table_raw_text_fallback_min_rows
        and table.cols >= profile.table_raw_text_fallback_min_cols
        and table.cols <= profile.table_raw_text_fallback_max_cols
    )


def _append_raw_table_region(
    page_parts: list[str],
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> tuple[int, int]:
    region_parts, region_chunks, region_cards = _recognize_image_region(
        engine,
        image,
        profile,
    )
    page_parts.extend(part for part in region_parts if part.strip())
    return region_chunks, region_cards


def _append_sparse_table_raw_fallback(
    page_parts: list[str],
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> int:
    raw_text = engine.recognize(
        image,
        mode="text_mode",
        psm=profile.table_raw_text_fallback_psm,
    )
    fallback_calls = 0
    if profile.sparse_text_fallback_engine and (
        _engine_name(engine) != profile.sparse_text_fallback_engine
    ):
        fallback_engine = _create_sparse_text_fallback_engine(profile)
        if fallback_engine is not None:
            fallback_calls = 1
            fallback_text = fallback_engine.recognize(
                image,
                mode="text_mode",
                psm=profile.table_raw_text_fallback_psm,
            )
            fallback_tokens = _ocr_token_count(fallback_text)
            primary_tokens = _ocr_token_count(raw_text)
            min_fallback_tokens = max(
                profile.sparse_text_fallback_min_tokens,
                int(primary_tokens * profile.table_raw_text_fallback_min_ratio),
            )
            if fallback_tokens >= min_fallback_tokens:
                raw_text = (
                    fallback_text
                    if not raw_text.strip()
                    else "\n\n".join(dedupe_chunks([raw_text, fallback_text]))
                )
    if not raw_text.strip():
        return 1 + fallback_calls

    normalized_existing = " ".join("\n\n".join(page_parts).split())
    normalized_raw = " ".join(raw_text.split())
    if normalized_raw and normalized_raw not in normalized_existing:
        page_parts.append(raw_text)
    return 1 + fallback_calls


async def convert(
    path: Path,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
    pdf_mode: str = "auto",
) -> Tuple[str, dict]:
    return convert_bytes(
        path.read_bytes(),
        filename=path.name,
        engine_type=engine_type,
        pipeline_profile=pipeline_profile,
        pdf_mode=pdf_mode,
    )


def convert_bytes(
    content: bytes,
    filename: str,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
    pdf_mode: str = "auto",
) -> Tuple[str, dict]:
    markdown_parts = []
    meta = None
    for event in iter_convert_bytes(
        content,
        filename=filename,
        engine_type=engine_type,
        pipeline_profile=pipeline_profile,
        pdf_mode=pdf_mode,
    ):
        if event["type"] == "page" and event["markdown"].strip():
            markdown_parts.append(event["markdown"])
        elif event["type"] == "complete":
            meta = event["meta"]

    if meta is None:
        raise ValueError("OCR conversion did not produce completion metadata.")
    return "\n\n---\n\n".join(markdown_parts), meta


def _create_engine(engine_type: str, profile: OcrPipelineProfile):
    if engine_type == "auto":
        return AutoEngine(
            prefer_tesseract=True,
            tesseract_language_priority=profile.tesseract_language_priority,
            tesseract_ocr_border_pixels=profile.ocr_border_pixels,
            tesseract_edge_word_fallback_psms=profile.edge_word_fallback_psms,
        )
    if engine_type == "tesseract":
        from app.engines.tesseract_engine import TesseractEngine

        return TesseractEngine(
            language_priority=profile.tesseract_language_priority,
            ocr_border_pixels=profile.ocr_border_pixels,
            edge_word_fallback_psms=profile.edge_word_fallback_psms,
        )
    if engine_type == "easyocr":
        from app.engines.easyocr_engine import EasyOcrEngine

        engine = EasyOcrEngine()
        if not engine.available():
            raise ValueError(
                f"EasyOCR is not installed or initialization failed: {engine.info().get('init_error')}"
            )
        return engine
    raise ValueError(
        f"Unknown OCR engine '{engine_type}'. Known engines: auto, easyocr, tesseract"
    )


def _convert_layout_region(
    region: LayoutRegion,
    engine,
    profile: OcrPipelineProfile,
    layout_parameters: tuple[tuple[str, FeatureValue], ...] = (),
) -> tuple[list[str], dict]:
    page_parts = []
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0

    if region.kind == "table" and region.table is not None:
        if _should_segment_table_region(region.image, region.table):
            region_parts, region_chunks, region_cards = _recognize_image_region(
                engine,
                region.image,
                profile,
            )
            return (
                [part for part in region_parts if part.strip()],
                {
                    "chunks": region_chunks,
                    "cards_found": region_cards,
                    "tables_found": 0,
                    "table_cells": 0,
                },
            )

        if profile.table_layout_normalization == "preserve_grid":
            table_layout = region.table
        elif profile.table_layout_normalization == "logical_columns":
            table_layout = logical_table_layout(
                region.image,
                region.table,
            )
        else:
            raise ValueError(
                "Unknown table layout normalization "
                f"'{profile.table_layout_normalization}'"
            )
        tables_found += 1
        table_cells += len(table_layout.cells)
        table_md = ""
        table_words, table_word_calls = _recognize_table_words(
            engine,
            region.image,
            table_layout,
            profile,
            strategy=profile.table_word_recognition,
        )
        total_chunks += table_word_calls
        if table_words:
            word_cell_coverage = _table_word_cell_coverage(
                table_layout,
                table_words,
            )
            for formatter_name in profile.table_word_formatters:
                min_coverage = (
                    profile.wide_table_min_word_cell_coverage
                    if formatter_name == "curriculum"
                    else profile.table_min_word_cell_coverage
                )
                if word_cell_coverage < min_coverage:
                    continue
                table_md = format_table_words(
                    formatter_name,
                    table_layout,
                    table_words,
                )
                if table_md.strip():
                    break

        if not table_md.strip():
            if len(table_layout.cells) > profile.max_table_cell_ocr_calls:
                if _should_append_table_raw_text_fallback(profile, table_layout):
                    total_chunks += _append_sparse_table_raw_fallback(
                        page_parts,
                        engine,
                        region.image,
                        profile,
                    )
                else:
                    region_chunks, region_cards = _append_raw_table_region(
                        page_parts,
                        engine,
                        region.image,
                        profile,
                    )
                    total_chunks += region_chunks
                    cards_found += region_cards
                return (
                    page_parts,
                    {
                        "chunks": total_chunks,
                        "cards_found": cards_found,
                        "tables_found": tables_found,
                        "table_cells": table_cells,
                    },
                )

            cell_ocr_calls = 0

            def recognize_cell(cell_image: Image.Image) -> str:
                nonlocal cell_ocr_calls
                cell_ocr_calls += 1
                return _recognize_table_cell(engine, cell_image)

            cell_rows = table_layout_to_rows(
                region.image,
                table_layout,
                recognize_cell,
            )
            total_chunks += cell_ocr_calls
            if (
                _table_row_cell_coverage(table_layout, cell_rows)
                >= profile.table_min_cell_coverage
            ):
                table_md = table_rows_to_markdown(cell_rows)
        if table_md.strip():
            page_parts.append(table_md)
        else:
            if _should_append_table_raw_text_fallback(profile, table_layout):
                total_chunks += _append_sparse_table_raw_fallback(
                    page_parts,
                    engine,
                    region.image,
                    profile,
                )
            else:
                region_chunks, region_cards = _append_raw_table_region(
                    page_parts,
                    engine,
                    region.image,
                    profile,
                )
                total_chunks += region_chunks
                cards_found += region_cards
        if table_md.strip() and _should_append_table_raw_text_fallback(
            profile, table_layout
        ):
            total_chunks += _append_sparse_table_raw_fallback(
                page_parts,
                engine,
                region.image,
                profile,
            )
        return (
            page_parts,
            {
                "chunks": total_chunks,
                "cards_found": cards_found,
                "tables_found": tables_found,
                "table_cells": table_cells,
            },
        )

    if dict(layout_parameters).get("direct_region_ocr") is True:
        region_parts = [
            _recognize_text_with_sparse_fallback(
                engine,
                region.image,
                profile,
                mode="text_mode",
                psm=_text_psm_for_image_region(region.image, profile),
            )
        ]
        region_chunks = 1
        region_cards = 0
    else:
        region_parts, region_chunks, region_cards = _recognize_image_region(
            engine,
            region.image,
            profile,
        )
    return (
        [part for part in region_parts if part.strip()],
        {
            "chunks": region_chunks,
            "cards_found": region_cards,
            "tables_found": 0,
            "table_cells": 0,
        },
    )


@contextmanager
def _owned_layout_regions(
    regions: list[LayoutRegion],
    source_image: Image.Image,
):
    try:
        yield regions
    finally:
        for region in regions:
            if region.image is not source_image:
                region.image.close()


def _convert_page_segment(
    image: Image.Image,
    engine,
    profile: OcrPipelineProfile,
) -> tuple[str, dict]:
    page_parts = []
    totals = {
        "chunks": 0,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    if _is_dewarped_projector_slide(image) or _looks_like_edge_to_edge_word(image):
        layout_parameters = ()
        regions = [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]
    elif profile.layout.feature_extractors:
        regions, layout_decision = analyze_layout(
            image,
            profile.layout,
            min_confirmed_cell_ratio=profile.grid_min_confirmed_cell_ratio,
        )
        layout_parameters = (
            layout_decision.stages[0].parameters if layout_decision.stages else ()
        )
    else:
        layout_parameters = ()
        regions = (
            analyze_document_layout(
                image,
                min_confirmed_cell_ratio=profile.grid_min_confirmed_cell_ratio,
            )
            if "table_regions" in profile.layout.allowed_stages
            else [
                LayoutRegion(
                    kind="image",
                    image=image,
                    bbox=(0, 0, *image.size),
                )
            ]
        )

    with _owned_layout_regions(regions, image) as owned_regions:
        for region in owned_regions:
            region_parts, meta = _convert_layout_region(
                region,
                engine,
                profile,
                layout_parameters,
            )
            page_parts.extend(region_parts)
            for key in totals:
                totals[key] += meta[key]
            if region.image is not image:
                region.image.close()

    return (
        MarkdownFormatter.format_text("\n\n".join(page_parts)),
        totals,
    )


def _convert_page(
    main_image: Image.Image,
    engine,
    profile: OcrPipelineProfile,
) -> tuple[str, dict]:
    width, height = main_image.size
    is_long_screenshot = (
        height >= LONG_SCREENSHOT_MIN_HEIGHT
        and height / max(1, width) >= LONG_SCREENSHOT_MIN_ASPECT_RATIO
    )
    if not is_long_screenshot:
        markdown, totals = _convert_page_segment(main_image, engine, profile)
        fallback_text = ""
        fallback_calls = 0
        if profile.dense_grid_fallback:
            if _is_dewarped_projector_slide(main_image):
                fallback_text = _recognize_projector_slide_fallback(
                    engine,
                    main_image,
                    profile,
                )
                fallback_calls = 1
            elif _looks_like_dense_grid_page(main_image):
                fallback_text, fallback_calls = _recognize_dense_grid_page(
                    engine,
                    main_image,
                    profile,
                )
            elif _looks_like_sparse_cover_page(main_image):
                fallback_text, fallback_calls = _recognize_sparse_cover_page(
                    engine,
                    main_image,
                    profile,
                )
            totals["chunks"] += fallback_calls
            if fallback_text.strip():
                markdown = MarkdownFormatter.format_text(
                    "\n\n".join(
                        dedupe_chunks(
                            [part for part in (markdown, fallback_text) if part.strip()]
                        )
                    )
                )
        return markdown, totals

    page_parts = []
    totals = {
        "chunks": 0,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    segments = split_vertical(main_image, chunk_height=1600, overlap=120)
    try:
        for segment in segments:
            markdown, meta = _convert_page_segment(segment, engine, profile)
            if markdown.strip():
                page_parts.append(markdown)
            for key in totals:
                totals[key] += meta[key]
    finally:
        for segment in segments:
            if segment is not main_image:
                segment.close()

    return MarkdownFormatter.format_text("\n\n".join(page_parts)), totals


def iter_convert_bytes(
    content: bytes,
    filename: str,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
    pdf_mode: str = "auto",
) -> Iterator[dict]:
    """
    Convert a document page by page and yield page/completion events.

    Args:
        content: Uploaded document bytes.
        filename: Original filename used to distinguish PDF from images.
        engine_type: 'auto' (Tesseract first), 'tesseract' (core), or 'easyocr' (high-quality)
        pdf_mode: 'auto' uses a trustworthy PDF text layer before OCR;
            'raster' always renders PDF pages for OCR.
    """
    profile = pipeline_profile or resolve_pipeline_profile(engine_type)
    normalized_pdf_mode = normalize_pdf_mode(pdf_mode)
    text_layer_pages = _extract_pdf_text_layer_pages(content, filename) if normalized_pdf_mode == "auto" else []
    if text_layer_pages:
        for page_number, page_text in enumerate(text_layer_pages, start=1):
            yield {
                "type": "page",
                "page": page_number,
                "markdown": page_text,
            }
        yield {
            "type": "complete",
            "meta": {
                "engine": "pdf_text_layer",
                "engine_chain": ["pdf_text_layer"],
                "chunks": 0,
                "cards_found": 0,
                "tables_found": 0,
                "table_cells": 0,
                "pages": len(text_layer_pages),
                "empty_pages": [],
                "pipeline": profile.name,
                "pdf_mode": normalized_pdf_mode,
                "flags": sorted(profile_flags(profile)),
                "preprocess_steps": [],
                "layout_steps": [],
                "elapsed_ms": 0,
            },
        }
        return

    image_pipeline = OcrPreprocessingPipeline.from_step_names(
        profile.image_preprocessing
    )
    engine = _create_engine(engine_type, profile)
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0
    page_count = 0
    empty_pages = []

    for main_image in _iter_document_images(content, filename, image_pipeline):
        try:
            page_count += 1
            print(f"[OCR] Processing page {page_count}", flush=True)
            page_markdown, page_meta = _convert_page(main_image, engine, profile)
            total_chunks += page_meta["chunks"]
            cards_found += page_meta["cards_found"]
            tables_found += page_meta["tables_found"]
            table_cells += page_meta["table_cells"]
            if not page_markdown.strip():
                empty_pages.append(page_count)
                yield {
                    "type": "warning",
                    "code": "EMPTY_PAGE",
                    "message": f"No text was recognized on page {page_count}.",
                    "page": page_count,
                }
            yield {
                "type": "page",
                "page": page_count,
                "markdown": page_markdown,
            }
        finally:
            main_image.close()

    if page_count == 0:
        raise ValueError("Could not load image or parsed zero pages.")

    meta = {
        "engine": engine.info()["engine"],
        "engine_chain": _engine_chain(engine, profile),
        "chunks": total_chunks,
        "cards_found": cards_found,
        "tables_found": tables_found,
        "table_cells": table_cells,
        "pages": page_count,
        "empty_pages": empty_pages,
        "pipeline": profile.name,
        "pdf_mode": normalized_pdf_mode,
        "flags": sorted(profile_flags(profile)),
        "preprocess_steps": list(profile.image_preprocessing),
        "layout_steps": list(profile.layout.allowed_stages),
        "elapsed_ms": 0,  # to be overwritten in router
    }
    yield {"type": "complete", "meta": meta}
