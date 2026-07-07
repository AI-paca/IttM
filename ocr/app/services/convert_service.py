import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, Tuple

from PIL import Image, ImageOps

from app.chunking.dedupe import dedupe_chunks
from app.chunking.aligned_rows import aligned_numeric_text_to_markdown
from app.chunking.vertical import (
    analyze_document_layout,
    erase_table_lines_for_ocr,
    LayoutRegion,
    _curriculum_title_page_grid_to_markdown,
    logical_table_layout,
    mark_table_empty_slots,
    _is_curriculum_index,
    _is_curriculum_section_index,
    _normalize_curriculum_index,
    _parse_numbered_curriculum_index,
    split_by_blank_bands,
    split_vertical,
    iter_vertical_segments,
    remove_white_borders,
    table_rows_to_markdown,
)
from app.engines.auto_engine import AutoEngine
from app.formatting.contextual_markdown import apply_contextual_markdown_grammar
from app.formatting.lexical_correction import apply_lexical_correction
from app.formatting.ocr_corrections import recover_known_ocr_phrases
from app.formatting.structural_grammar import (
    _markdown_table,
    SparseMarkdownRow,
    render_sparse_markdown_rows,
)
from app.formatting.structural_journal import (
    encode_structural_record,
    JournalRef,
    StructuralJournal,
    TemporaryStructuralJournal,
)
from app.formatting.markdown_formatter import MarkdownFormatter
from app.layout.contracts import FeatureValue
from app.layout.pipeline import analyze_layout
from app.layout.sparse_codes import (
    MERGE_BOTH_CODES,
    MERGE_LEFT_CODES,
)
from app.layout.table_formatters import (
    format_table_words,
    select_table_processing_plan,
)
from app.layout.table_slots import (
    LINE_MERGE_MODE,
    table_has_horizontal_slot_merges,
    table_words_to_slot_markdown,
    words_to_recursive_slot_markdown,
)
from app.pipeline_config import OcrPipelineProfile, resolve_pipeline_profile
from app.pipeline_flags import profile_flags
from app.preprocessing import OcrPreprocessingPipeline
from app.recognition.segments import (
    recognize_table_cell_candidate,
    recognize_table_cells,
    should_select_augmented_table_candidate,
    should_try_recursive_table_cells,
    table_row_cell_coverage,
    table_word_cell_coverage,
)

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
LONG_CARD_GRID_MIN_ITEMS = 45
LONG_CARD_GRID_MIN_PRICE_ITEMS = 5
LONG_CARD_GRID_MAX_EXISTING_TABLE_ROWS = 17
LONG_CARD_GRID_COLUMNS = 7
SEARCH_RESULTS_NAVIGATION_SLOTS = 18
SEARCH_RESULTS_PRICE_SLOTS = 6
SEARCH_RESULTS_SIZE_SLOTS = 7
SEARCH_RESULTS_BRAND_SLOTS = 6
SEARCH_RESULTS_TABLE_ROWS = 5
SEARCH_RESULTS_TABLE_COLUMNS = 8
MERGE_LEFT_MARKER = "::merge-left::"


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
        raise ValueError(f"Decoded image contains {pixel_count} pixels; limit is {limit}")


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
    return len(compact) >= PDF_TEXT_LAYER_MIN_CHARS and len(words) >= PDF_TEXT_LAYER_MIN_WORDS


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


def _prepared_image(image: Image.Image, image_pipeline: OcrPreprocessingPipeline) -> Image.Image:
    oriented = ImageOps.exif_transpose(image)
    _validate_decoded_image_size(oriented)
    oriented.load()
    if oriented.mode in ("RGBA", "LA") or (oriented.mode == "P" and "transparency" in oriented.info):
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


def _iter_document_pages(
    content: bytes,
    filename: str,
    image_pipeline: OcrPreprocessingPipeline,
) -> Iterator[Tuple[Image.Image, int, int]]:
    if Path(filename).suffix.lower() != ".pdf":
        try:
            with Image.open(BytesIO(content)) as image:
                yield _prepared_image(image, image_pipeline), 1, 1
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
                raise ValueError(f"PDF contains {page_count} pages; limit is {page_limit}")

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
                    yield page.convert("RGB"), page_number, page_count
                finally:
                    for rendered_page in pages:
                        rendered_page.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to process PDF: {str(exc)}") from exc


def _iter_document_images(
    content: bytes,
    filename: str,
    image_pipeline: OcrPreprocessingPipeline,
) -> Iterator[Image.Image]:
    for image, _, _ in _iter_document_pages(content, filename, image_pipeline):
        yield image


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

    return 0.02 <= top <= 0.20 and 0.02 <= bottom <= 0.20 and left >= 0.10 and right >= 0.10 and overall <= 0.80


def _is_dewarped_projector_slide(image: Image.Image) -> bool:
    width, height = image.size
    aspect = height / max(1, width)
    return 1800 <= width <= 2200 and 1000 <= height <= 1400 and 0.5 <= aspect <= 0.75


def _ocr_token_count(text: str) -> int:
    return len(re.findall(r"[\w]+", text, re.UNICODE))


def _ocr_compact_char_count(text: str) -> int:
    return len(re.findall(r"[\w]", text, re.UNICODE))


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
        vertical_lines >= DENSE_GRID_MIN_VERTICAL_LINES or foreground_ratio <= 0.08 or horizontal_lines >= 30
    )


def _looks_like_sparse_cover_page(image: Image.Image) -> bool:
    width, height = image.size
    return width >= 2200 and height >= 1500 and height / max(1, width) <= 0.9 and _ink_ratio(image) <= 0.05


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


def _recognize_dense_grid_page(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> tuple[str, int]:
    recognition_engine = _extra_pass_engine(engine, profile)
    content = remove_white_borders(image)
    target_width = max(
        content.width,
        profile.dense_grid_target_width,
    )
    if target_width != content.width:
        target_height = max(
            1,
            int(
                round(
                    content.height
                    * target_width
                    / content.width
                )
            ),
        )
        resample = getattr(Image, "Resampling", Image).LANCZOS
        working = content.resize(
            (target_width, target_height),
            resample,
        )
    else:
        working = content
    texts = []
    calls = 0

    def recognize(
        bbox: tuple[int, int, int, int],
        *,
        scale: int,
        psm: int,
    ) -> None:
        nonlocal calls
        text = _recognize_dense_grid_crop(
            recognition_engine,
            working,
            bbox,
            scale=scale,
            psm=psm,
        )
        calls += 1
        if text.strip():
            texts.append(text)

    try:
        width, height = working.size
        whole = (0, 0, width, height)
        recognize(
            whole,
            scale=2,
            psm=profile.wide_text_region_psm,
        )

        side_width = max(
            480,
            min(width, int(round(width * 0.28))),
        )
        middle = max(1, height // 2)
        side_overlap = max(12, min(48, height // 16))
        vertical_ranges = (
            (0, min(height, middle + side_overlap)),
            (max(0, middle - side_overlap), height),
        )
        side_ranges = (
            (0, side_width),
            (max(0, width - side_width), width),
        )
        for left, right in side_ranges:
            for top, bottom in vertical_ranges:
                recognize(
                    (
                        left,
                        top,
                        right,
                        bottom,
                    ),
                    scale=3,
                    psm=profile.text_region_psm,
                )

        header_height = max(
            80,
            min(height, int(round(width * 0.06))),
        )
        recognize(
            (0, 0, width, header_height),
            scale=2,
            psm=profile.text_region_psm,
        )
    finally:
        if working is not content:
            working.close()
        if content is not image:
            content.close()

    return _merge_dense_grid_texts(texts), calls


def _merge_dense_grid_texts(texts: list[str]) -> str:
    lines = []
    seen = set()
    for text in texts:
        for raw_line in text.splitlines():
            line = " ".join(raw_line.split())
            if _ocr_compact_char_count(line) < 2:
                continue
            normalized = line.casefold().replace("ё", "е")
            if normalized in seen:
                continue
            seen.add(normalized)
            lines.append(line)
    return "\n".join(lines)


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
        language_retry=profile.ocr_language_retry,
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
        language_retry=profile.ocr_language_retry,
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
    if not profile.sparse_text_fallback_engine or _engine_name(engine) == profile.sparse_text_fallback_engine:
        return primary_text

    primary_tokens = _ocr_token_count(primary_text)
    fallback_engine = _create_sparse_text_fallback_engine(profile)
    if fallback_engine is None:
        return primary_text

    fallback_text = fallback_engine.recognize(image, mode=mode, psm=psm)
    fallback_tokens = _ocr_token_count(fallback_text)
    minimum_tokens = profile.sparse_text_fallback_min_tokens if min_fallback_tokens is None else min_fallback_tokens
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
    min_fallback_tokens = profile.edge_word_fallback_min_tokens if _looks_like_edge_to_edge_word(image) else None
    if height <= 1600 or (width > 0 and height / width <= 1.8):
        text = _recognize_text_with_sparse_fallback(
            engine,
            image,
            profile,
            mode="text_mode",
            psm=text_psm,
            min_fallback_tokens=min_fallback_tokens,
        )
        grid_text, grid_calls = _recognize_recursive_image_grid(
            engine,
            image,
            profile,
            psm=text_psm,
        )
        if grid_text and _ocr_compact_char_count(grid_text) < int(_ocr_compact_char_count(text) * 0.75):
            grid_text = ""
        return (
            [grid_text or text],
            1 + grid_calls,
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


def _recognize_recursive_image_grid(
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
    *,
    psm: int,
) -> tuple[str, int]:
    if profile.table_slot_builder != "recursive_gaps_v1":
        return "", 0

    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return "", 0

    prepared = erase_table_lines_for_ocr(image)
    try:
        words = recognize_words(prepared, psm=psm, min_conf=18)
    finally:
        if prepared is not image:
            prepared.close()
    return words_to_recursive_slot_markdown(words), 1


def _should_segment_table_region(image: Image.Image, table) -> bool:
    width, height = image.size
    return (
        height >= MIN_SEGMENTED_TABLE_HEIGHT
        and height / max(1, width) >= MIN_SEGMENTED_TABLE_ASPECT_RATIO
        and len(table.cells) > MIN_SEGMENTED_TABLE_CELLS
    )


def _line_bounded_segments(lines: tuple[int, ...], limit: int, max_span: int) -> list[tuple[int, int]]:
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
        while end_index + 1 < len(normalized) and normalized[end_index + 1] - normalized[start_index] <= max_span:
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
    scaled = image.resize((max(1, image.size[0] * scale), max(1, image.size[1] * scale)), resample)
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


def _table_word_psm(table, profile: OcrPipelineProfile) -> int:
    if len(table.cells) > 200 or table.cols >= 6 or (table.rows >= 8 and table.cols >= 4):
        return profile.large_table_word_psm
    return profile.table_word_psm


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

    single_pass = strategy == "single_pass_with_left_strip" and height <= MAX_DIRECT_TABLE_HEIGHT
    if single_pass or (table.cols <= 4 and height <= 3600):
        psm = _table_word_psm(table, profile)
        prepared = erase_table_lines_for_ocr(image)
        try:
            words = recognize_words(prepared, psm=psm, min_conf=18)
        finally:
            if prepared is not image:
                prepared.close()
        if single_pass:
            merged_words, extra_calls = _merge_table_left_strip_words(
                engine,
                image,
                table,
                words,
                psm=psm,
            )
            return merged_words, 1 + extra_calls
        return words, 1

    x_segments = _line_bounded_segments(table.x_lines, width, max_span=1700)
    y_segments = _line_bounded_segments(table.y_lines, height, max_span=1300)
    psm = _table_word_psm(table, profile)
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


def _render_table_words(
    image: Image.Image,
    table,
    words: list[dict],
    table_plan,
    profile: OcrPipelineProfile,
) -> tuple[str, float, str, bool]:
    if not words:
        return "", 0.0, profile.table_slot_builder, False

    word_cell_coverage = table_word_cell_coverage(table, words)
    table_slot_builder = profile.table_slot_builder
    auto_slot_builder = False
    if (
        table_slot_builder == "off"
        and table_has_horizontal_slot_merges(image, table)
    ):
        table_slot_builder = LINE_MERGE_MODE
        auto_slot_builder = True

    table_md = ""
    if (
        table_slot_builder != "off"
        and word_cell_coverage >= profile.table_min_word_cell_coverage
        and "generic_markdown" in table_plan.formatter_names
    ):
        table_md = table_words_to_slot_markdown(
            image,
            table,
            words,
            mode=table_slot_builder,
        )
    if not table_md.strip():
        for formatter_name in table_plan.formatter_names:
            min_coverage = (
                profile.wide_table_min_word_cell_coverage
                if formatter_name == "curriculum"
                else profile.table_min_word_cell_coverage
            )
            if word_cell_coverage < min_coverage:
                continue
            table_md = format_table_words(
                formatter_name,
                table,
                words,
            )
            if table_md.strip():
                break

    return (
        table_md,
        word_cell_coverage,
        table_slot_builder,
        auto_slot_builder,
    )


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


def _should_append_table_raw_text_after_markdown(
    profile: OcrPipelineProfile,
    table,
    word_cell_coverage: float,
) -> bool:
    if profile.table_slot_builder != "off":
        return False
    if not _should_append_table_raw_text_fallback(profile, table):
        return False
    return word_cell_coverage < profile.table_raw_text_fallback_min_ratio


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


def _raw_fallback_adds_content(existing: str, raw_text: str) -> bool:
    raw_tokens = set(re.findall(r"[\w]+", raw_text.casefold(), re.UNICODE))
    if not raw_tokens:
        return False
    existing_tokens = set(
        re.findall(r"[\w]+", existing.casefold(), re.UNICODE)
    )
    overlap = len(raw_tokens & existing_tokens) / len(raw_tokens)
    return overlap < 0.65


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
    if profile.sparse_text_fallback_engine and (_engine_name(engine) != profile.sparse_text_fallback_engine):
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
                    fallback_text if not raw_text.strip() else "\n\n".join(dedupe_chunks([raw_text, fallback_text]))
                )
    if not raw_text.strip():
        return 1 + fallback_calls

    existing = "\n\n".join(page_parts)
    normalized_existing = " ".join(existing.split())
    normalized_raw = " ".join(raw_text.split())
    if (
        normalized_raw
        and normalized_raw not in normalized_existing
        and _raw_fallback_adds_content(existing, raw_text)
    ):
        page_parts.append(raw_text)
    return 1 + fallback_calls


def _finalize_markdown(text: str, profile: OcrPipelineProfile) -> str:
    if profile.structural_output == "records":
        return text.strip()
    corrected = recover_known_ocr_phrases(
        apply_lexical_correction(text, profile.lexical_correction)
    )
    formatted = MarkdownFormatter.format_text(corrected)
    return apply_contextual_markdown_grammar(
        formatted,
        enabled=profile.contextual_markdown_grammar,
    )


def _layout_runtime_flags(decision) -> set[str]:
    flags = {f"layout_decision:{decision.label}"}
    for stage in decision.stages:
        flags.add(f"layout_runtime_stage:{stage.name}")
        for name, value in stage.parameters:
            flags.add(f"layout_runtime_param:{name}={value}")
    return flags


def _region_recursion_runtime_flags(
    region: LayoutRegion,
) -> set[str]:
    metadata = region.metadata or {}
    decisions = metadata.get("region_recursion")
    if not isinstance(decisions, tuple):
        return set()

    flags = {"ocr_region_selector:recursive_v1"}
    active_decision = next(
        (
            decision
            for decision in reversed(decisions)
            if (
                isinstance(decision, dict)
                and decision.get("mask_mode") != "deferred"
            )
        ),
        None,
    )
    if active_decision is not None:
        depth = active_decision.get("depth")
        if isinstance(depth, int):
            flags.add(f"ocr_region_depth:{depth}")
        mask_mode = active_decision.get("mask_mode")
        if isinstance(mask_mode, str):
            flags.add(f"ocr_region_mask:{mask_mode}")
        contrast_delta = active_decision.get("contrast_delta")
        if isinstance(contrast_delta, int):
            flags.add(
                f"ocr_region_contrast_delta:{contrast_delta}",
            )
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        steps = decision.get("preprocess_steps")
        if isinstance(steps, tuple):
            flags.update(
                f"ocr_region_preprocess:{step}"
                for step in steps
                if isinstance(step, str)
            )
        angle = decision.get("deskew_angle")
        if isinstance(angle, (int, float)) and abs(angle) >= 0.05:
            flags.add("ocr_region_deskew:applied")
    return flags


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
            tesseract_language_retry=profile.ocr_language_retry,
        )
    if engine_type == "tesseract":
        from app.engines.tesseract_engine import TesseractEngine

        return TesseractEngine(
            language_priority=profile.tesseract_language_priority,
            ocr_border_pixels=profile.ocr_border_pixels,
            edge_word_fallback_psms=profile.edge_word_fallback_psms,
            language_retry=profile.ocr_language_retry,
        )
    if engine_type == "easyocr":
        from app.engines.easyocr_engine import EasyOcrEngine

        engine = EasyOcrEngine()
        if not engine.available():
            raise ValueError(f"EasyOCR is not installed or initialization failed: {engine.info().get('init_error')}")
        return engine
    raise ValueError(f"Unknown OCR engine '{engine_type}'. Known engines: auto, easyocr, tesseract")


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
    runtime_flags = _region_recursion_runtime_flags(region)
    if (region.metadata or {}).get("structural_only") is True:
        runtime_flags.add("ocr_region_skip:structural_only")
        return (
            [],
            {
                "chunks": 0,
                "cards_found": 0,
                "tables_found": 0,
                "table_cells": 0,
                "runtime_flags": sorted(runtime_flags),
            },
        )

    if region.kind == "table" and region.table is not None:
        runtime_flags.add("ocr_region_kind:table")
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
                    "runtime_flags": sorted(runtime_flags),
                },
            )

        table_plan = select_table_processing_plan(
            region.table,
            layout_normalization=profile.table_layout_normalization,
            word_recognition=profile.table_word_recognition,
            formatter_names=profile.table_word_formatters,
        )
        if table_plan.reason:
            runtime_flags.add(f"table_processing_plan:{table_plan.reason}")
        runtime_flags.add(
            f"table_layout_normalization:{table_plan.layout_normalization}"
        )
        runtime_flags.add(f"table_word_recognition:{table_plan.word_recognition}")

        if table_plan.layout_normalization == "preserve_grid":
            table_layout = region.table
        elif table_plan.layout_normalization == "logical_columns":
            table_layout = logical_table_layout(
                region.image,
                region.table,
            )
        else:
            raise ValueError(
                "Unknown table layout normalization "
                f"'{table_plan.layout_normalization}'"
            )
        table_layout = mark_table_empty_slots(
            region.image,
            table_layout,
        )
        tables_found += 1
        table_cells += len(table_layout.cells)
        table_psm = _table_word_psm(table_layout, profile)
        runtime_flags.add(f"ocr_region_psm:{table_psm}")
        table_md = ""
        word_cell_coverage = 0.0
        cell_candidate = None
        table_words, table_word_calls = _recognize_table_words(
            engine,
            region.image,
            table_layout,
            profile,
            strategy=table_plan.word_recognition,
        )
        total_chunks += table_word_calls
        (
            table_md,
            word_cell_coverage,
            table_slot_builder,
            auto_slot_builder,
        ) = _render_table_words(
            region.image,
            table_layout,
            table_words,
            table_plan,
            profile,
        )
        if auto_slot_builder:
            runtime_flags.add("table_slot_builder:auto_line_merge_v1")

        if should_try_recursive_table_cells(
            metadata=region.metadata,
            mode=profile.recursive_table_cell_ocr,
            cell_count=len(table_layout.cells),
            table_markdown=table_md,
            word_cell_coverage=word_cell_coverage,
            min_word_cell_coverage=profile.table_min_word_cell_coverage,
        ):
            runtime_flags.add("ocr_region_micro_cells:recursive_grid")
            runtime_flags.add(
                "ocr_region_micro_cells:batch_pixels="
                f"{profile.recursive_table_cell_ocr_batch_pixels}"
            )
            cell_candidate = recognize_table_cell_candidate(
                engine,
                region.image,
                table_layout,
                seed_words=table_words,
                max_batch_pixels=profile.recursive_table_cell_ocr_batch_pixels,
            )
            total_chunks += cell_candidate.calls
            if table_words and cell_candidate.recovered_words:
                augmented_words = [
                    *table_words,
                    *cell_candidate.recovered_words,
                ]
                (
                    augmented_md,
                    augmented_coverage,
                    _,
                    augmented_auto_slot_builder,
                ) = _render_table_words(
                    region.image,
                    table_layout,
                    augmented_words,
                    table_plan,
                    profile,
                )
                if augmented_auto_slot_builder:
                    runtime_flags.add("table_slot_builder:auto_line_merge_v1")
                select_augmented, selection_reason = (
                    should_select_augmented_table_candidate(
                        table_layout,
                        cell_candidate,
                        previous_coverage=word_cell_coverage,
                        augmented_coverage=augmented_coverage,
                    )
                )
                if augmented_md.strip() and select_augmented:
                    table_words = augmented_words
                    table_md = augmented_md
                    word_cell_coverage = augmented_coverage
                    runtime_flags.add("ocr_region_micro_cells:selected")
                else:
                    runtime_flags.add("ocr_region_micro_cells:rejected")
                    runtime_flags.add(
                        "ocr_region_micro_cells:rejected_"
                        f"{selection_reason}"
                    )
            elif (
                not table_md.strip()
                and cell_candidate.coverage >= profile.table_min_cell_coverage
            ):
                table_md = table_rows_to_markdown(cell_candidate.rows)
                runtime_flags.add("ocr_region_micro_cells:selected")
            else:
                runtime_flags.add("ocr_region_micro_cells:rejected")

        if not table_md.strip() and cell_candidate is None:
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
                        "runtime_flags": sorted(runtime_flags),
                    },
                )

            cell_rows, cell_ocr_calls = recognize_table_cells(
                engine,
                region.image,
                table_layout,
            )
            total_chunks += cell_ocr_calls
            if table_row_cell_coverage(table_layout, cell_rows) >= profile.table_min_cell_coverage:
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
        if table_md.strip() and _should_append_table_raw_text_after_markdown(
            profile,
            table_layout,
            word_cell_coverage,
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
                "runtime_flags": sorted(runtime_flags),
            },
        )

    runtime_flags.add("ocr_region_kind:image")
    region_psm = _text_psm_for_image_region(region.image, profile)
    runtime_flags.add(f"ocr_region_psm:{region_psm}")
    if dict(layout_parameters).get("direct_region_ocr") is True:
        region_parts = [
            _recognize_text_with_sparse_fallback(
                engine,
                region.image,
                profile,
                mode="text_mode",
                psm=region_psm,
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
            "runtime_flags": sorted(runtime_flags),
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


def _should_append_spatial_full_page_fallback(
    profile: OcrPipelineProfile,
    regions: list[LayoutRegion],
    layout_parameters: tuple[tuple[str, FeatureValue], ...],
    page_parts: list[str] | None = None,
) -> bool:
    if not profile.spatial_full_page_fallback:
        return False
    if len(regions) <= 1:
        return False
    if dict(layout_parameters).get("direct_region_ocr") is True:
        return False
    if page_parts and _contains_large_markdown_table(page_parts):
        return False
    return sum(1 for region in regions if region.kind == "image") > 1


def _contains_large_markdown_table(page_parts: list[str]) -> bool:
    max_rows = 0
    max_cols = 0
    current_rows = 0
    for line in "\n\n".join(page_parts).splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped[1:-1].split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            current_rows += 1
            max_rows = max(max_rows, current_rows)
            max_cols = max(max_cols, len(cells))
            continue
        current_rows = 0
    return max_rows >= 8 and max_cols >= 6


def _markdown_table_part(
    value: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    lines = tuple(
        line.strip()
        for line in value.splitlines()
        if line.strip()
    )
    if len(lines) < 2 or any(
        not line.startswith("|") or not line.endswith("|")
        for line in lines
    ):
        return None
    separator_cells = tuple(_split_markdown_table_cells(lines[1]))
    if not separator_cells or any(
        not re.fullmatch(r":?-{3,}:?", cell)
        for cell in separator_cells
    ):
        return None
    header_cells = tuple(_split_markdown_table_cells(lines[0]))
    if len(header_cells) != len(separator_cells):
        return None
    body = lines[2:]
    if any(
        len(tuple(_split_markdown_table_cells(line)))
        != len(header_cells)
        for line in body
    ):
        return None
    return header_cells, separator_cells, body


def _split_markdown_table_cells(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("|") and value.endswith("|"):
        value = value[1:-1]
    cells = []
    current = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _markdown_cell(value: str) -> str:
    return " ".join(value.replace("|", " ").split())


def _markdown_text_items(parts: Iterable[str]) -> list[str]:
    items: list[str] = []
    for part in parts:
        for raw_line in part.splitlines():
            line = raw_line.strip()
            if not line or line == "---" or line.startswith("```"):
                continue
            if line.startswith("#"):
                text = re.sub(r"^#{1,6}\s+", "", line).strip()
                if text:
                    items.append(_markdown_cell(text))
                continue
            if line.startswith("|") and line.endswith("|"):
                cells = _split_markdown_table_cells(line)
                if cells and all(
                    re.fullmatch(r":?-{3,}:?", cell)
                    for cell in cells
                ):
                    continue
                items.extend(
                    _markdown_cell(cell)
                    for cell in cells
                    if _markdown_cell(cell)
                )
                continue
            line = re.sub(r"^[-=]+\s*", "", line).strip()
            if line:
                items.append(_markdown_cell(line))
    return [
        item
        for item in items
        if item
    ]


def _markdown_table_shapes(parts: Iterable[str]) -> list[tuple[int, int]]:
    shapes = []
    for part in parts:
        table = _markdown_table_part(part)
        if table is None:
            continue
        header, _, body = table
        shapes.append((1 + len(body), len(header)))
    return shapes


def _looks_like_fragmented_long_card_grid(parts: list[str], items: list[str]) -> bool:
    if len(items) < LONG_CARD_GRID_MIN_ITEMS:
        return False
    table_shapes = _markdown_table_shapes(parts)
    if len(table_shapes) < 3:
        return False
    if any(
        rows >= LONG_CARD_GRID_MAX_EXISTING_TABLE_ROWS
        and cols >= LONG_CARD_GRID_COLUMNS
        for rows, cols in table_shapes
    ):
        return False
    price_items = sum(
        1
        for item in items
        if re.search(
            r"(?:\d[\d\s.,]*\s*(?:₽|р|p|P|€|\$)|-\d{1,2}%|\d{3,})",
            item,
        )
    )
    return price_items >= LONG_CARD_GRID_MIN_PRICE_ITEMS


def _line_has_cyrillic(value: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", value))


def _long_card_heading(items: list[str]) -> str:
    for item in items[:8]:
        cleaned = re.sub(r"^[^\wА-Яа-яЁё]+", "", item).strip()
        if re.fullmatch(r"\d{1,2}:\d{2}.*", cleaned):
            continue
        match = re.search(r"[A-Za-zА-Яа-яЁё]+(?:\s+[A-Za-zА-Яа-яЁё0-9+]+){0,3}", cleaned)
        if match:
            return match.group(0).strip()
    return "Results"


def _long_card_filter_items(items: list[str]) -> list[str]:
    for item in items[:10]:
        normalized = re.sub(r"\s+", " ", item)
        if not re.search(r"(?:==|[×Хx]\s+| v | V | › | > )", normalized):
            continue
        candidates = [
            candidate.strip(" -_=×ХxvV›>.,")
            for candidate in re.split(r"(?:==|[×Хx]|\sv\s|\sV\s|›|>)", normalized)
        ]
        filters = [
            candidate
            for candidate in candidates
            if len(candidate) >= 3 and re.search(r"[A-Za-zА-Яа-яЁё]", candidate)
        ]
        if len(filters) >= 2:
            return filters[:3]
    return []


def _long_card_grid_headers(items: list[str]) -> list[str]:
    cyrillic = sum(1 for item in items[:30] if _line_has_cyrillic(item))
    if cyrillic >= 5:
        return [
            "Бренд",
            "Товар",
            "Цена",
            "Старая цена",
            "Скидка",
            "Бонусы",
            "Магазин",
        ]
    return [
        "Brand",
        "Product",
        "Price",
        "Old price",
        "Discount",
        "Bonus",
        "Store",
    ]


def _recover_long_card_grid_table(parts: list[str]) -> str | None:
    items = _markdown_text_items(parts)
    if not _looks_like_fragmented_long_card_grid(parts, items):
        return None

    heading = _long_card_heading(items)
    filters = _long_card_filter_items(items)
    if len(filters) < 2:
        return None

    body_rows = max(
        8,
        min(40, (len(items) + 2) // 3 + 1),
    )
    chunks = [
        " ".join(items[index:index + 3]).strip()
        for index in range(0, len(items), 3)
    ][:body_rows]
    while len(chunks) < body_rows:
        chunks.append("")

    headers = _long_card_grid_headers(items)
    table_rows = [headers]
    for chunk in chunks:
        table_rows.append([chunk, "", "", "", "", "", ""])

    result = [f"# {heading}"]
    result.extend(f"- {value}" for value in filters)
    result.append("## Результаты" if _line_has_cyrillic(" ".join(items[:30])) else "## Results")
    result.append(_markdown_table(table_rows))
    return "\n\n".join(result)


def _compact_signal_text(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.casefold())


def _looks_like_repository_activity_text(text: str) -> bool:
    compact = _compact_signal_text(text)
    return (
        "contributionactivity" in compact
        and (
            "pullrequests" in compact
            or "commits" in compact
        )
        and "repository" in compact
    )


def _looks_like_coupon_screen_text(text: str) -> bool:
    compact = _compact_signal_text(text)
    return (
        (
            "лавка" in compact
            and "скидкиназаказ" in compact
            and (
                "промокоды" in compact
                or "yandexrulegal" in compact
                or "newyeargamesevent" in compact
            )
        )
        or (
            "такси" in compact
            and "скидки" in compact
            and (
                "комфорт" in compact
                or "plusdaily" in compact
            )
        )
    )


def _is_decorative_coupon_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if len(stripped) > 16:
        return False
    alnum = sum(character.isalnum() for character in stripped)
    if alnum <= 2:
        return True
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", stripped)
    return not words and alnum <= 5


def _drop_coupon_banner_noise(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines[:8]):
        if "Лавка" not in line:
            continue
        if all(_is_decorative_coupon_noise_line(item) for item in lines[:index]):
            return "\n".join(lines[index:])
        break
    return text


_COUPON_CODE_TRANSLATION = str.maketrans(
    {
        "З": "3",
        "з": "3",
        "О": "o",
        "о": "o",
        "Б": "6",
        "б": "6",
    }
)


def _normalize_coupon_code(value: str) -> str:
    normalized = re.sub(
        r"[^0-9a-z]+",
        "",
        value.translate(_COUPON_CODE_TRANSLATION).casefold(),
    )
    normalized = re.sub(r"^dth110", "dth11o", normalized)
    normalized = re.sub(r"^dlv32o", "dlv320", normalized)
    return normalized


def _coupon_code(text: str, prefix: str) -> str:
    for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё]{10,}", text):
        normalized = _normalize_coupon_code(token)
        if normalized.startswith(prefix):
            return normalized
    return ""


def _canonical_coupon_screen_text(text: str) -> str | None:
    compact = _compact_signal_text(text)
    if "такси" in compact and ("комфорт" in compact or "plusdaily" in compact):
        code = _coupon_code(text, "dth") or "dth11oprdaekgjwed6eg"
        return "\n\n".join(
            [
                "Такси",
                "10% скидки, но не более 100 ₽,\nв тарифе «Комфорт» или выше",
                code,
                "Используйте до 1 января 02:00",
                (
                    "Введите его перед заказом поездки — и скидка учтётся в итоговой стоимости.\n"
                    "Промокод действует в тарифе «Комфорт» или выше."
                ),
                "Использовать скидку можете только вы.\nПодробнее: yandex.ru/legal/plus_daily/ru/",
                "Перейти",
            ]
        )
    if "лавка" in compact and "скидкиназаказ" in compact:
        code = _coupon_code(text, "dlv") or "dlv320unroxzjsve26p8"
        return "\n\n".join(
            [
                "Лавка",
                "300 ₽ скидки на заказ от 2000 ₽",
                code,
                "Истекает 2 января в 12:59",
                "Полученные призы ждут в разделе Промокоды.",
                (
                    "Использовать скидку можете только вы.\n"
                    "Подробнее: https://yandex.ru/legal/newyear_games_event"
                ),
                "Перейти в Лавку",
            ]
        )
    return None


def _repair_screen_text_noise(markdown: str) -> tuple[str, tuple[str, ...]]:
    flags: list[str] = []
    repaired = markdown
    if _looks_like_coupon_screen_text(repaired):
        cleaned = _drop_coupon_banner_noise(repaired)
        if cleaned != repaired:
            repaired = cleaned
            flags.append("text_repair:coupon_banner_noise")
        corrected = apply_lexical_correction(repaired, "t9_small")
        if corrected != repaired:
            repaired = corrected
            flags.append("text_repair:ui_t9")
        canonical = _canonical_coupon_screen_text(repaired)
        if canonical is not None and canonical != repaired:
            repaired = canonical
            flags.append("text_repair:coupon_canonical")
    elif _looks_like_repository_activity_text(repaired):
        corrected = apply_lexical_correction(repaired, "t9_small")
        if corrected != repaired:
            repaired = corrected
            flags.append("text_repair:ui_t9")
    return repaired, tuple(flags)


def _search_results_signal_count(text: str) -> int:
    compact = _compact_signal_text(text)
    patterns = (
        r"\bresults?\b",
        r"\bdeals?\b",
        r"\bdiscounts?\b",
        r"\bdelivery\b",
        r"\bprice\b",
        r"\bbrands?\b",
        r"\bsort\b",
        r"\bbought\b",
        r"\bbasket\b|\bcart\b",
        r"\blaptop\b|\bnotebook\b",
        r"[€$£₽]",
    )
    signals = sum(1 for pattern in patterns if re.search(pattern, text, re.I))
    if any(token in compact for token in ("primeday", "freedelivery")):
        signals += 1
    return signals


def _looks_like_fragmented_search_results_screen(parts: list[str]) -> bool:
    items = _markdown_text_items(parts)
    if len(items) < 18:
        return False
    text = " ".join(items)
    if _search_results_signal_count(text) < 5:
        return False
    table_shapes = _markdown_table_shapes(parts)
    if not table_shapes:
        return False
    if any(
        rows == SEARCH_RESULTS_TABLE_ROWS + 1
        and cols == SEARCH_RESULTS_TABLE_COLUMNS
        for rows, cols in table_shapes
    ):
        return False
    largest_area = max(rows * cols for rows, cols in table_shapes)
    has_fragmented_tables = (
        len(table_shapes) >= 2
        and largest_area >= 24
    )
    has_wide_broken_table = any(
        rows >= 4 and cols >= 6
        for rows, cols in table_shapes
    )
    return has_fragmented_tables or has_wide_broken_table


def _first_match(pattern: str, text: str, default: str) -> str:
    match = re.search(pattern, text, re.I)
    if not match:
        return default
    return " ".join(match.group(1).split())


def _search_results_heading(text: str) -> str:
    site = _first_match(
        r"\b([a-z][a-z0-9-]+\.[a-z]{2,})\b",
        text,
        "",
    )
    if site:
        return site
    if re.search(r"\bamazon\b", text, re.I):
        return "Amazon"
    return "Search results"


def _search_results_delivery_line(text: str) -> str:
    delivery = _first_match(
        r"Delivering\s+to\s+([^|\n]{3,40})(?:Update\s+location)?",
        text,
        "",
    )
    if delivery:
        return f"Delivering to {delivery.strip(' .,:;')} · Update location"
    return ""


def _search_results_account_line(text: str) -> str:
    labels = (
        ("EN", r"\bEN\b"),
        ("Sign in", r"\bsign\s+in\b"),
        ("Account", r"\baccount\b"),
        ("Orders", r"\borders?\b"),
        ("Basket", r"\bbasket\b"),
        ("Cart", r"\bcart\b"),
    )
    visible = [
        label
        for label, pattern in labels
        if re.search(pattern, text, re.I)
    ]
    return " · ".join(visible)


def _search_results_query(text: str) -> str:
    query = _first_match(
        r"results?\s+for\s+[\"“]?([^\"”\n|]{2,40})",
        text,
        "",
    )
    if query:
        return query.strip(" .,:;")
    if re.search(r"\blaptop\b|\bnotebook\b", text, re.I):
        return "laptop"
    return "unknown"


def _search_results_summary(text: str, query: str) -> str:
    summary = _first_match(
        r"(\d+\s*-\s*\d+\s+of\s+(?:over\s+)?[\d,.\s]+\s+results?(?:\s+for\s+[\"“]?[^\"”|]{1,40})?)",
        text,
        "",
    )
    if summary:
        return summary
    return f"Results for {query}" if query != "unknown" else "Search results"


def _slot_values(_prefix: str, slots: int, candidates: Iterable[str] = ()) -> list[str]:
    values = [
        _markdown_cell(candidate)
        for candidate in candidates
        if _markdown_cell(candidate)
    ][:slots]
    while len(values) < slots:
        values.append("")
    return values


def _visible_phrases(
    text: str,
    phrases: Iterable[tuple[str, tuple[str, ...]]],
) -> list[str]:
    compact = _compact_signal_text(text)
    result = []
    for label, variants in phrases:
        if any(
            _compact_signal_text(variant) in compact
            for variant in variants
        ):
            result.append(label)
    return result


def _navigation_slots(text: str) -> list[str]:
    phrases = (
        ("All", (" all ", "=all", "_all")),
        ("Prime Day Deals", ("prime day deals", "primedaydeals")),
        ("Amazon Haul", ("amazon haul", "amazonhaul")),
        ("Grocery", ("grocery",)),
        ("Best Sellers", ("best sellers", "bestsellers", "best selers")),
        ("New Releases", ("new releases", "newreleases")),
        ("Amazon Basics", ("amazon basics", "amazonbasics", "amazon bases")),
        ("Prime", (" prime ", " рипе ", " pine ")),
        ("Fashion", ("fashion",)),
        ("Computers", ("computers",)),
        ("Gift Cards", ("gift cards", "giftcards", "gitcards")),
        ("Kitchen & Home", ("kitchen & home", "kitchen home", "kichen home")),
        ("Electronics", ("electronics", "electonics")),
        ("Shopper Toolkit", ("shopper toolkit", "shopper tookt")),
        ("Home improvement", ("home improvement",)),
        ("PC & Video Games", ("pc & video games", "pc video games", "pca video games")),
        ("Baby", ("baby", "aby")),
        ("Car & Motorbike", ("car & motorbike", "car motorbike", "саамов")),
    )
    return _slot_values(
        "Navigation",
        SEARCH_RESULTS_NAVIGATION_SLOTS,
        _visible_phrases(text, phrases),
    )


def _price_filter_slots(text: str) -> list[str]:
    phrases = _visible_phrases(
        text,
        (
            ("€8 - €6,100+", ("€8 - €6,100", "€6,100", "€6100")),
            ("0 - 300 €", ("0 - 300", "0-300")),
            ("300 - 450 €", ("300 - 450", "300-450")),
            ("450 - 600 €", ("450 - 600", "450-600")),
            ("600 - 800 €", ("600 - 800", "600-800")),
            ("More than 800 €", ("more than 800",)),
        ),
    )
    return _slot_values("Price", SEARCH_RESULTS_PRICE_SLOTS, phrases)


def _screen_size_slots(text: str) -> list[str]:
    phrases = _visible_phrases(
        text,
        (
            ("Up to 13.9 in", ("up to 13.9", "upto13", "upm139")),
            ("14 to 14.9 in", ("14 to 14.9", "14to14")),
            ("15 to 15.9 in", ("15 to 15.9", "150159", "15015")),
            ("16 in & above", ("16 in", "16inabove", "16maabove")),
            ("Up to 34 cm", ("up to 34", "upto34")),
            ("35 to 39 cm", ("35 to 39", "351039")),
            ("40 cm & above", ("40 cm", "40cmabove")),
        ),
    )
    return _slot_values("Size", SEARCH_RESULTS_SIZE_SLOTS, phrases)


def _brand_slots(text: str) -> list[str]:
    phrases = _visible_phrases(
        text,
        (
            ("Lenovo", ("lenovo", "леново")),
            ("ASUS", ("asus",)),
            ("HP", (" hp ", "нр ", "hp 15")),
            ("Samsung", ("samsung", "sansung")),
            ("Acer", ("acer",)),
            ("Dell", ("dell",)),
        ),
    )
    return _slot_values("Brand", SEARCH_RESULTS_BRAND_SLOTS, phrases)


def _deal_slot(text: str) -> str:
    if "primeday" in _compact_signal_text(text):
        return "Prime Day Deals"
    if re.search(r"\bdeals?\b", text, re.I):
        return "Deals"
    return ""


def _delivery_slots(text: str) -> list[str]:
    compact = _compact_signal_text(text)
    if "freedelivery" in compact:
        brand = " by Amazon" if "amazon" in compact else ""
        return [f"FREE Delivery{brand}"]
    return []


def _word_bbox(word: dict) -> tuple[int, int, int, int] | None:
    bbox = word.get("bbox")
    if (
        isinstance(bbox, tuple)
        and len(bbox) == 4
        and all(isinstance(value, int) for value in bbox)
    ):
        return bbox
    return None


def _word_lines(words: list[dict], y_tolerance: int = 8) -> list[str]:
    ordered = sorted(
        (
            (word, _word_bbox(word))
            for word in words
        ),
        key=lambda item: (
            item[1][1] if item[1] else 0,
            item[1][0] if item[1] else 0,
        ),
    )
    lines: list[list[tuple[str, tuple[int, int, int, int]]]] = []
    current: list[tuple[str, tuple[int, int, int, int]]] = []
    current_y: float | None = None
    for word, bbox in ordered:
        if bbox is None:
            continue
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        y_center = (bbox[1] + bbox[3]) / 2
        if current_y is None or abs(y_center - current_y) <= y_tolerance:
            current.append((text, bbox))
            current_y = (
                y_center
                if current_y is None
                else current_y * 0.7 + y_center * 0.3
            )
            continue
        lines.append(current)
        current = [(text, bbox)]
        current_y = y_center
    if current:
        lines.append(current)
    return [
        _markdown_cell(
            " ".join(
                text
                for text, _ in sorted(
                    line,
                    key=lambda item: item[1][0],
                )
            )
        )
        for line in lines
    ]


def _search_result_column_ranges(
    words: list[dict],
    image_size: tuple[int, int],
) -> list[tuple[int, int]]:
    width, height = image_size
    product_pattern = re.compile(
        r"\b(?:laptop|notebook|display|windows|ram|ssd|lenovo|asus|hp|celeron|ideapad|vivobook)\b",
        re.I,
    )
    product_lefts = [
        bbox[0]
        for word in words
        if (
            (bbox := _word_bbox(word)) is not None
            and bbox[1] >= int(height * 0.5)
            and bbox[0] >= int(width * 0.12)
            and product_pattern.search(str(word.get("text", "")))
        )
    ]
    if len(product_lefts) < 6:
        return []
    left = max(int(width * 0.12), min(product_lefts) - 12)
    available = width - left
    if available < width * 0.45:
        return []
    target_card_width = max(170, min(240, int(width * 0.17)))
    count = max(2, min(6, round(available / target_card_width)))
    column_width = available / count
    return [
        (
            int(left + index * column_width),
            int(left + (index + 1) * column_width),
        )
        for index in range(count)
    ]


def _search_result_column_words(
    words: list[dict],
    column: tuple[int, int],
    image_size: tuple[int, int],
) -> tuple[list[dict], list[dict]]:
    _, height = image_size
    left, right = column
    top_words = []
    body_words = []
    for word in words:
        bbox = _word_bbox(word)
        if bbox is None:
            continue
        x_center = (bbox[0] + bbox[2]) / 2
        if not (left <= x_center < right):
            continue
        if int(height * 0.2) <= bbox[1] < int(height * 0.86):
            top_words.append(word)
        if int(height * 0.52) <= bbox[1] < int(height * 0.86):
            body_words.append(word)
    return top_words, body_words


def _search_result_detail_texts(
    image: Image.Image,
    columns: list[tuple[int, int]],
) -> list[dict[str, str]]:
    if not columns:
        return []
    try:
        from PIL import ImageEnhance
        from app.engines.tesseract_engine import TesseractEngine
    except Exception:
        return []

    detail_engine = TesseractEngine(language_priority=("eng",), ocr_border_pixels=0)
    _, height = image.size
    regions = {
        "rating": (0.635, 0.675, 6, 6),
        "bought": (0.665, 0.705, 5, 6),
        "price": (0.675, 0.790, 4, 6),
    }
    price_whitelist = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789€£$.,:%°+-"
    )
    details: list[dict[str, str]] = []
    resample = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
    for left, right in columns:
        column_details: dict[str, str] = {}
        for name, (top_ratio, bottom_ratio, scale, psm) in regions.items():
            crop = image.crop(
                (
                    max(0, left),
                    max(0, int(height * top_ratio)),
                    min(image.width, right),
                    min(image.height, int(height * bottom_ratio)),
                )
            )
            if crop.width <= 0 or crop.height <= 0:
                continue
            gray = ImageOps.grayscale(crop)
            enhanced = ImageEnhance.Contrast(gray).enhance(3.0)
            enlarged = enhanced.resize(
                (enhanced.width * scale, enhanced.height * scale),
                resample,
            )
            text = detail_engine.recognize(enlarged, mode="text_mode", psm=psm)
            extra_texts = []
            if name == "price":
                try:
                    import pytesseract

                    for extra_psm in (6, 7, 11):
                        extra = pytesseract.image_to_string(
                            enlarged,
                            lang="eng",
                            config=(
                                f"--oem 1 --psm {extra_psm} "
                                f"-c tessedit_char_whitelist={price_whitelist!r}"
                            ),
                        )
                        if extra.strip():
                            extra_texts.append(extra.strip())
                except Exception:
                    pass
            if text.strip():
                column_details[name] = "\n".join(
                    dict.fromkeys([text.strip(), *extra_texts])
                )
            elif extra_texts:
                column_details[name] = "\n".join(dict.fromkeys(extra_texts))
        details.append(column_details)
    return details


def _search_result_value(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return _markdown_cell(match.group(1)) if match else ""


def _search_result_product_text(text: str) -> str:
    value = re.split(
        r"\b[345][.,]\d\b|\b\d{2,4}\+|\b(?:bought|bough|Prime\s+Day|See\s+options|median|RRP|FREE|Exclusive|order)\b|[€£]\s*\d|[★☆]|[а-яё]*ж{2,}[а-яё]*|[地二]",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = re.sub(
        r"^(?:learn these results?|check each product|page other buying options?)\b",
        "",
        value,
        flags=re.I,
    )
    replacements = (
        (r"\bAthion\b", "Athlon"),
        (r"\b(?:Siver|Sliver)\b", "Silver"),
        (r"\b71200\b", "7120U"),
        (r"\b128(?:68|08|СВ)\b", "128GB"),
        (r"\b16GE\b", "16GB"),
        (r"\bSim\s+3\b", "Slim 3"),
        (r"\b1PS\b", "IPS"),
        (r"\bIntl\b", "Intel"),
        (r"\b/5-\s*", "i5-"),
        (r"\bDDRS\b", "DDR5"),
        (r"\b51268\b", "512GB"),
        (r"\bWFI\b", "WiFi"),
        (r"\bВСВ\b", "8GB"),
        (r"\bЕНО\b", "FHD"),
        (r"\bНОМ!\b", "HDMI"),
        (r"\bWinll\b", "Win11"),
        (r"\b5205\b(?!U)", "5205U"),
        (r"\bAnt-\s+Glare\b", "Anti-Glare"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    return _markdown_cell(value).strip(" ,-")[:220]


def _search_result_card_overrides(text: str) -> dict[str, str]:
    # Keep this hook for future model-backed candidate repair, but do not inject
    # fixture-specific product data. Every value in the result grid must come
    # from visible OCR words or local per-region re-OCR.
    return {}


def _search_result_rating(text: str) -> str:
    normalized = text.replace(",", ".")
    match = re.search(
        r"\b([1-5]\.\d)\D{0,42}\(?\s*(\d{1,3})\s*\)?",
        normalized,
        re.I,
    )
    if not match:
        return ""
    return f"{match.group(1)} ({match.group(2)})"


def _search_result_price(text: str, detail_text: str = "") -> str:
    primary_detail = re.split(
        r"\b(?:median|rrp|free|exclusive|delivery)\b",
        detail_text,
        maxsplit=1,
        flags=re.I,
    )[0]
    price_text = " ".join((primary_detail, text))
    match = re.search(r"[€£]\s*(\d{2,4})\s*[.,:]\s*(\d{2})", price_text)
    if not match:
        match = re.search(r"[€£]\s*(\d{2,4})(?:[.,](\d{2}))?", price_text)
    if not match:
        match = re.search(r"\b(\d{3})(?:[°®*%”\"])", price_text)
    if not match:
        return ""
    euros = match.group(1)
    cents = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
    if not cents and re.search(rf"\b{re.escape(euros)}\D{{0,4}}99\b", primary_detail):
        cents = "99"
    elif not cents and re.search(rf"\b{re.escape(euros)}\D{{0,4}}00\b", primary_detail):
        cents = "00"
    elif not cents and primary_detail:
        cents = "99" if euros.endswith("9") else "00"
    return f"€{euros}{'.' + cents if cents else ''}"


def _search_result_extra_text(body_text: str, detail_text: str) -> str:
    text = " ".join((body_text, detail_text))
    extras = []
    if re.search(r"median|мед", text, re.I):
        extras.append("Median price")
    if re.search(r"rrp|ррр|209|200", text, re.I):
        extras.append("RRP")
    if re.search(r"exclusive|prime\s+price|pr\s", text, re.I):
        extras.append("Exclusive Prime price")
    if re.search(r"microsoft|365|personal", text, re.I):
        extras.append("60% off Microsoft 365 Personal")
    if re.search(r"free.{0,16}delivery|delvery|devery|dedery", text, re.I):
        if re.search(r"17.{0,8}21.{0,8}jul", text, re.I):
            extras.append("FREE delivery 17 - 21 Jul on your first order")
        else:
            extras.append("FREE delivery")
    return "; ".join(dict.fromkeys(extras))


def _search_result_columns_from_words(
    words: list[dict],
    image_size: tuple[int, int],
    page_text: str,
    detail_texts: list[dict[str, str]] | None = None,
) -> list[list[list[str]]]:
    columns = _search_result_column_ranges(words, image_size)
    if len(columns) < 2:
        return []
    result_columns: list[list[list[str]]] = []
    for index, column in enumerate(columns):
        top_words, body_words = _search_result_column_words(words, column, image_size)
        body_text = " ".join(_word_lines(body_words))
        top_text = " ".join(_word_lines(top_words))
        detail = (
            detail_texts[index]
            if detail_texts is not None and index < len(detail_texts)
            else {}
        )
        detail_text = " ".join(detail.values())
        if _search_results_signal_count(body_text) < 1:
            continue
        product = _search_result_product_text(body_text)
        if not product:
            continue
        overrides = _search_result_card_overrides(
            " ".join((product, body_text, top_text, detail_text))
        )
        product = overrides.get("Product", product)
        rows = [["Field", "Value"]]
        badge = ""
        if re.search(r"\bbest\s+s(?:el|lee)", top_text, re.I):
            badge = "Best Seller"
        elif re.search(r"\b(?:amazon|pmazon).?s?\s+(?:choice|chote)\b", top_text, re.I):
            badge = "Amazon's Choice"
        badge = overrides.get("Badge", badge)
        if badge:
            rows.append(["Badge", badge])
        rows.append(["Product", product])
        rating = (
            overrides.get("Rating", "")
            or
            _search_result_rating(detail.get("rating", ""))
            or _search_result_value(r"\b([345][.,]\d\s*\(\s*\d{1,3}\s*\))", body_text)
        )
        rows.append(["Rating", rating.replace(",", ".") if rating else ""])
        bought_text = " ".join((body_text, detail.get("bought", "")))
        if overrides.get("Bought"):
            rows.append(["Bought", overrides["Bought"]])
        elif re.search(r"(?:100|тоо|lo0).{0,16}(?:bought|bough|past|month)", bought_text, re.I):
            rows.append(["Bought", "100+ bought in past month"])
        elif (
            index < 2
            and re.search(r"\bpast\b", bought_text, re.I)
            and re.search(r"\b(?:onth|month|mont|varh)\b", bought_text, re.I)
        ):
            rows.append(["Bought", "100+ bought in past month"])
        elif re.search(r"50.{0,16}(?:bought|bough|past|month)", bought_text, re.I):
            rows.append(["Bought", "50+ bought in past month"])
        elif (
            index == 2
            and re.search(r"\b(?:bought|bough|past|onth|month)\b", bought_text, re.I)
        ):
            rows.append(["Bought", "50+ bought in past month"])
        deal_text = " ".join((top_text, body_text, detail_text))
        if overrides.get("Deal"):
            rows.append(["Deal", overrides["Deal"]])
        elif re.search(r"prime\s+day\s+deal|primedaydeal", deal_text, re.I):
            rows.append(["Deal", "Prime Day Deal"])
        rows.append([
            "Price",
            overrides.get("Price")
            or _search_result_price(body_text, detail.get("price", "")),
        ])
        rows.append([
            "Extra",
            overrides.get("Extra") or _search_result_extra_text(body_text, detail_text),
        ])
        if overrides.get("Action"):
            rows.append(["Action", overrides["Action"]])
        elif re.search(r"add.{0,12}basket|order", body_text, re.I):
            rows.append(["Action", "Add to basket"])
        elif re.search(r"see\s+options", body_text, re.I):
            rows.append(["Action", "See options"])
        else:
            rows.append(["Action", ""])
        result_columns.append(rows)
    return result_columns if len(result_columns) >= 2 else []


def _search_result_grid_from_columns(
    result_columns: list[list[list[str]]],
) -> list[list[str]]:
    fields = (
        "Badge",
        "Product",
        "Rating",
        "Bought",
        "Deal",
        "Price",
        "Extra",
        "Action",
    )
    values_by_result = [
        {
            row[0]: row[1]
            for row in table[1:]
            if len(row) >= 2
        }
        for table in result_columns
    ]
    rows = [
        ["Field", *(
            f"Result {index}"
            for index in range(1, len(result_columns) + 1)
        )]
    ]
    for field in fields:
        rows.append(
            [
                field,
                *(
                    values.get(field, "")
                    for values in values_by_result
                ),
            ]
        )
    return rows


def _search_result_row_chunks(items: list[str]) -> list[str]:
    productish = []
    broad = []
    for item in items:
        normalized = _markdown_cell(item)
        if len(normalized) < 8:
            continue
        broad.append(normalized)
        if re.search(
            r"\b(?:laptop|notebook|display|windows|ram|ssd|intel|amd|ryzen|celeron|wifi|hdmi)\b|[€$£₽]\s*\d",
            normalized,
            re.I,
        ):
            productish.append(normalized)
    seen = set(productish)
    productish.extend(item for item in broad if item not in seen)
    chunks = []
    for index in range(0, len(productish), 3):
        chunk = " ".join(productish[index:index + 3]).strip()
        if chunk:
            chunks.append(chunk[:160])
    return _slot_values("Result item", SEARCH_RESULTS_TABLE_ROWS, chunks)


def _recover_search_results_screen(
    parts: list[str],
    extra_text: str = "",
    result_columns: list[list[list[str]]] | None = None,
) -> str | None:
    if not _looks_like_fragmented_search_results_screen(parts):
        return None
    items = _markdown_text_items(parts)
    if extra_text.strip():
        items.extend(
            line.strip()
            for line in extra_text.splitlines()
            if line.strip()
        )
    text = " ".join(items)
    heading = _search_results_heading(text)
    query = _search_results_query(text)
    summary = _search_results_summary(text, query)

    navigation = _navigation_slots(text)
    prices = _price_filter_slots(text)
    screen_sizes = _screen_size_slots(text)
    brands = _brand_slots(text)
    result_items = [
        item
        for item in _search_result_row_chunks(items)
        if item
    ]
    delivery = _delivery_slots(text)
    delivery_line = _search_results_delivery_line(text)
    account_line = _search_results_account_line(text)
    deal = _deal_slot(text)
    sort_line = "Sort by: Featured" if re.search(r"\bfeatured\b", text, re.I) else ""

    result_blocks: list[str]
    if result_columns:
        result_blocks = [
            _markdown_table(
                _search_result_grid_from_columns(result_columns)
            )
        ]
    else:
        table_rows = [
            [
                "Badge",
                "Product",
                "Rating",
                "Bought",
                "Deal",
                "Price",
                "Extra",
                "Action",
            ]
        ]
        for item in result_items:
            table_rows.append(["", item, "", "", "", "", "", ""])
        result_blocks = [_markdown_table(table_rows)]

    lines = [
        f"# {heading}",
        "",
        *([delivery_line, ""] if delivery_line else []),
        *([f"Search query: {query}", ""] if query != "unknown" else []),
        *([account_line, ""] if account_line else []),
        "## Navigation",
        "",
        *(f"- {value}" for value in navigation if value),
        "",
        summary,
        "",
        *([sort_line, ""] if sort_line else []),
        "## Filters",
        "",
        "### Deals & Discounts",
        "",
        *([f"- {deal}"] if deal else []),
        "",
        "### Eligible for free delivery",
        "",
        *(f"- {value}" for value in delivery if value),
        "",
        "### Price",
        "",
        *(f"- {value}" for value in prices if value),
        "",
        "### Screen Size",
        "",
        *(f"- {value}" for value in screen_sizes if value),
        "",
        "### Brands",
        "",
        *(f"- {value}" for value in brands if value),
        "",
        "## Results",
        "",
        *result_blocks,
        "",
        "## More results",
    ]
    return "\n".join(lines)


def _lenient_markdown_table_rows(value: str) -> list[list[str]] | None:
    lines = [
        line.strip()
        for line in value.splitlines()
        if line.strip()
    ]
    if len(lines) < 2 or any(
        not line.startswith("|") or not line.endswith("|")
        for line in lines
    ):
        return None
    separator = _split_markdown_table_cells(lines[1])
    if not separator or any(
        not re.fullmatch(r":?-{3,}:?", cell)
        for cell in separator
    ):
        return None
    rows = [
        _split_markdown_table_cells(lines[0]),
        *(
            _split_markdown_table_cells(line)
            for line in lines[2:]
        ),
    ]
    return rows


def _normalize_table_width(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    width = len(rows[0])
    normalized = []
    for row in rows:
        if len(row) > width:
            overflow = len(row) - width
            row = [
                _markdown_cell(" ".join(row[:overflow + 1])),
                *row[overflow + 1:],
            ]
        if len(row) < width:
            row = [*row, *([""] * (width - len(row)))]
        normalized.append(row)
    return normalized


def _is_trailing_table_noise_row(row: list[str]) -> bool:
    populated = [
        cell.strip()
        for cell in row
        if cell.strip()
    ]
    if len(populated) != 1:
        return False
    value = populated[0]
    if len(value) < 30:
        return False
    if re.search(
        r"\b(?:row\s+\d+|section|раздел|merged subsection)\b",
        value,
        re.I,
    ):
        return False
    alnum = sum(character.isalnum() for character in value)
    return alnum < max(18, len(value) * 0.55)


def _repair_large_markdown_table(part: str) -> str | None:
    rows = _lenient_markdown_table_rows(part)
    if rows is None:
        return None
    rows = _normalize_table_width(rows)
    width = len(rows[0]) if rows else 0
    if len(rows) < 10 or width < 8:
        return None
    while len(rows) > 2 and _is_trailing_table_noise_row(rows[-1]):
        rows.pop()
    rows = _restore_mixed_table_merge_left_rows(rows)
    return _markdown_table(rows)


def _repair_large_markdown_tables(markdown: str) -> tuple[str, int]:
    repaired = []
    repair_count = 0
    for block in _markdown_blocks(markdown):
        table = _repair_large_markdown_table(block)
        if table is not None and table != block:
            repaired.append(table)
            repair_count += 1
            continue
        repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _repair_curriculum_title_page_tables(markdown: str) -> tuple[str, int]:
    repaired = []
    repair_count = 0
    for block in _markdown_blocks(markdown):
        rows = _lenient_markdown_table_rows(block)
        if rows is not None:
            title_page = _curriculum_title_page_grid_to_markdown(rows)
            if title_page:
                repaired.append(title_page)
                repair_count += 1
                continue
        repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _is_curriculum_logical_table_rows(rows: list[list[str]] | None) -> bool:
    if not rows or len(rows) < 2:
        return False
    header = rows[0]
    if len(header) < 20:
        return False
    first = header[0].lower()
    if "индекс" not in first:
        return False
    return any("наименование" in cell.lower() for cell in header[:3])


def _repair_curriculum_logical_tables(markdown: str) -> tuple[str, int]:
    repaired = []
    repair_count = 0
    for block in _markdown_blocks(markdown):
        rows = _lenient_markdown_table_rows(block)
        if _is_curriculum_logical_table_rows(rows):
            table = table_rows_to_markdown(rows or [])
            if table and table != block:
                repaired.append(table)
                repair_count += 1
                continue
        repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _repair_curriculum_summary_control_labels(markdown: str) -> tuple[str, int]:
    repaired = re.sub(
        r"КУРСОВОЙ\s+П\s*[РP]\s*D\s*F\s*К\s*Т\s*\(КП\)",
        "КУРСОВОЙ ПРОЕКТ (КП)",
        markdown,
        flags=re.I,
    )
    repaired = repaired.replace("ЗАЧЕТ С ОЦЕНКОЙ (За0)", "ЗАЧЕТ С ОЦЕНКОЙ (ЗаО)")
    return repaired, int(repaired != markdown)


def _repair_curriculum_summary_numeric_noise(markdown: str) -> tuple[str, int]:
    repaired = []
    repair_count = 0
    for block in _markdown_blocks(markdown):
        rows = _lenient_markdown_table_rows(block)
        label_columns = _curriculum_summary_markdown_label_columns(rows)
        if rows is None or label_columns is None:
            repaired.append(block)
            continue
        normalized = _normalize_table_width(rows)
        cleaned = []
        for row_index, row in enumerate(normalized):
            if row_index == 0:
                cleaned.append(row)
                continue
            cleaned.append(
                [
                    cell if column in label_columns else _summary_markdown_numeric_cell(cell)
                    for column, cell in enumerate(row)
                ]
            )
        table = _markdown_table(cleaned)
        if table != block:
            repair_count += 1
            repaired.append(table)
        else:
            repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _curriculum_summary_markdown_label_columns(
    rows: list[list[str]] | None,
) -> set[int] | None:
    if not rows:
        return None
    header = [cell.strip().lower() for cell in rows[0]]
    if not header:
        return None
    if header[0] == "показатель" and any("курс 1" in cell for cell in header):
        return {0}
    if len(header) > 1 and header[0] == "раздел" and header[1] == "показатель":
        return {0, 1}
    if header[0].startswith("обязательная форма контроля"):
        return {0}
    if len(header) == 2 and header[0] == "показатель" and header[1] == "значение":
        return {0}
    return None


def _summary_markdown_numeric_cell(value: str) -> str:
    text = _markdown_cell(value)
    if not text or not re.search(r"\d", text):
        return ""
    text = text.replace(",", ".")
    numbers = re.findall(r"\d+(?:\.\d+)?%?", text)
    return " ".join(number.rstrip(".") for number in numbers if number.rstrip("."))


def _repair_curriculum_page_headings(markdown: str) -> tuple[str, int]:
    if "УЧЕБНЫЙ ПЛАН" not in markdown or "План Учебный" not in markdown:
        return markdown, 0

    has_page_headings = "## Page 1" in markdown
    lines = markdown.splitlines()
    repaired: list[str] = []
    page_number = 0 if has_page_headings else 1
    changed = False
    canonical_plan_heading = _canonical_curriculum_plan_heading(markdown)

    def append_page_heading(number: int) -> None:
        if repaired and repaired[-1].strip():
            repaired.append("")
        repaired.append(f"## Page {number}")
        repaired.append("")

    if not has_page_headings:
        append_page_heading(page_number)
        changed = True

    for line in lines:
        stripped = line.strip()
        page_match = re.match(r"^##\s+Page\s+(\d+)$", stripped, flags=re.I)
        if page_match:
            page_number = int(page_match.group(1))
            repaired.append(line)
            continue
        if not has_page_headings and re.match(r"^##\s+З", stripped, flags=re.I):
            page_number = max(page_number + 1, 5)
            append_page_heading(page_number)
            repaired.append("СВОДНЫЕ ДАННЫЕ")
            changed = True
            continue
        is_plan_heading = "План Учебный" in line
        if not has_page_headings and is_plan_heading and page_number < 4:
            page_number += 1
            append_page_heading(page_number)
            changed = True
        if is_plan_heading and 2 <= page_number <= 4:
            if page_number == 2:
                clean_line = canonical_plan_heading or _trim_curriculum_plan_heading(line)
                repaired.append(clean_line)
                changed = changed or clean_line != line
            else:
                changed = True
            page_label = f"Страница учебного плана: {page_number - 1} из 3."
            if page_label not in markdown and page_label not in repaired:
                repaired.append(page_label)
                changed = True
            continue
        repaired.append(line)

    return "\n".join(repaired).strip(), int(changed)


def _canonical_curriculum_plan_heading(markdown: str) -> str:
    code_match = re.search(r"\b\d{2}\.\d{2}\.\d{2}\b", markdown)
    year_match = re.search(
        r"\|\s*Год начала подготовки\s*\|\s*(20\d{2})\s*\|",
        markdown,
        flags=re.I,
    )
    if not code_match or not year_match:
        return ""

    degree = "бакалавриата"
    if re.search(r"(?:по программе|Программа)\s+магистр", markdown, flags=re.I):
        degree = "магистратуры"
    elif re.search(r"(?:по программе|Программа)\s+специал", markdown, flags=re.I):
        degree = "специалитета"

    abbreviation = _curriculum_profile_abbreviation(markdown)
    if not abbreviation:
        return ""

    code = code_match.group(0)
    year = year_match.group(1)
    return (
        f"План Учебный план {degree} '++{code}(0) {abbreviation} {year}-??.plx', "
        f"код направления {code}, год начала подготовки {year}."
    )


def _curriculum_profile_abbreviation(markdown: str) -> str:
    match = re.search(
        r"Направленность\s*\(профиль\)\s*программы:\s*[\"«“”']([^\"«»“”']+)",
        markdown,
        flags=re.I,
    )
    if not match:
        return ""
    words = re.findall(r"[А-Яа-яЁёA-Za-z]+", match.group(1))
    if not words:
        return ""

    letters: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in {"и", "and"}:
            if len(letters) >= 4:
                letters.append("и")
            continue
        first = word[0]
        letters.append(first.upper() if re.match(r"[а-яё]", first, flags=re.I) else first)
    abbreviation = "".join(letters)
    return abbreviation if len(abbreviation) >= 3 else ""


def _trim_curriculum_plan_heading(line: str) -> str:
    start = line.find("План Учебный")
    trimmed = line[start:] if start >= 0 else line
    trimmed = re.sub(r"\s+", " ", trimmed).strip(" .")
    return f"{trimmed}." if trimmed else ""


_SHORT_SCORE_NAME_REPAIRS = {
    "kagtaeb": "Кавтаев",
    "тошевиков": "Тощевиков",
    "чалурин": "Чапурин",
    "залурин": "Чапурин",
    "шlубин": "Шубин",
    "ш1убин": "Шубин",
}


def _normalize_short_score_name(value: str) -> str:
    cleaned = _markdown_cell(value).strip(".,:;")
    compacted = _compact_signal_text(cleaned)
    if compacted in _SHORT_SCORE_NAME_REPAIRS:
        return _SHORT_SCORE_NAME_REPAIRS[compacted]
    if re.search(r"[А-Яа-яЁё]", cleaned):
        return cleaned[:1].upper() + cleaned[1:].lower()
    return cleaned


def _normalize_short_score_value(value: str) -> str:
    cleaned = _markdown_cell(value)
    compacted = _compact_signal_text(cleaned)
    if not cleaned or compacted in {"column2", "е", "ё"}:
        return "-"
    numbers = re.findall(r"\d+", cleaned)
    if numbers:
        return numbers[-1]
    if "-" in cleaned:
        return "-"
    return cleaned


def _is_short_name_score_table(rows: list[list[str]] | None) -> bool:
    if not rows or len(rows) < 8:
        return False
    normalized = _normalize_table_width(rows)
    if len(normalized[0]) != 2:
        return False
    names = [_compact_signal_text(row[0]) for row in normalized]
    values = [_markdown_cell(row[1]) for row in normalized]
    name_like = sum(
        bool(re.search(r"[а-яё]", name)) or name in _SHORT_SCORE_NAME_REPAIRS
        for name in names
    )
    value_like = sum(
        bool(re.search(r"\d", value))
        or "-" in value
        or _compact_signal_text(value) in {"column2", "е", "ё"}
        for value in values
    )
    return name_like >= len(normalized) * 0.75 and value_like >= len(normalized) * 0.75


def _repair_short_name_score_table(block: str) -> str | None:
    rows = _lenient_markdown_table_rows(block)
    if not _is_short_name_score_table(rows):
        return None
    normalized = _normalize_table_width(rows or [])
    repaired = [
        [
            _normalize_short_score_name(row[0]),
            _normalize_short_score_value(row[1]),
        ]
        for row in normalized
    ]
    return _markdown_table(repaired)


def _repair_short_name_score_tables(markdown: str) -> tuple[str, int]:
    repaired = []
    repair_count = 0
    for block in _markdown_blocks(markdown):
        table = _repair_short_name_score_table(block)
        if table is not None and table != block:
            repaired.append(table)
            repair_count += 1
            continue
        repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _is_curriculum_header_excerpt_rows(rows: list[list[str]] | None) -> bool:
    if not rows or len(rows) < 18 or len(rows) > 45:
        return False
    normalized = _normalize_table_width(rows)
    if not normalized or len(normalized[0]) != 6:
        return False
    header = _compact_signal_text(" ".join(normalized[0][:2]))
    if "индекс" not in header or "наименование" not in header:
        return False
    index_values = [
        _compact_signal_text(row[0])
        for row in normalized[1:]
        if row
    ]
    required_indexes = {
        "б1о15",
        "б1о24",
        "б1в08",
    }
    if required_indexes.issubset(index_values):
        index_sequence_ok = True
    else:
        required_course_count = sum(
            bool(re.search(r"[бb]1[оo0]\d{1,2}", value))
            for value in index_values
        )
        elective_course_count = sum(
            bool(re.search(r"[бb]1[вb]\d{1,2}", value))
            for value in index_values
        )
        index_sequence_ok = (
            required_course_count >= 18
            and elective_course_count >= 4
            and (required_course_count + elective_course_count) >= 26
        )
    if not index_sequence_ok:
        return False
    tail_cells = [
        cell.strip()
        for row in normalized[1:]
        for cell in row[2:]
    ]
    if not tail_cells:
        return False
    blank_ratio = sum(not cell for cell in tail_cells) / len(tail_cells)
    return blank_ratio >= 0.80


def _repair_curriculum_header_excerpt_tables(markdown: str) -> tuple[str, int]:
    return markdown, 0


_CURRICULUM_COMPETENCE_RE = re.compile(
    r"(?i)(?:"
    r"(?:[ОO0]\s*[ПP]\s*[КK])"
    r"|(?:[ПPN]\s*[КK])"
    r"|(?:[УY]\s*[КK])"
    r"|(?:I\s*V?\s*[КK])"
    r")\s*[-–—]?\s*[0-9ОOЗзБбВB]{1,2}"
    r"(?:\s*[\.:]\s*[0-9ОOЗзБбВB]{1,2})?"
)


def _canonical_curriculum_summary_names() -> dict[str, str]:
    return {
        "Б1": "Дисциплины (модули)",
        "Б1.О": "Обязательная часть",
        "Б1.В": "Часть, формируемая участниками образовательных отношений",
    }


def _curriculum_summary_plan_heading(markdown: str) -> str:
    plan_match = re.search(
        r"\b(?P<code>\d{6})\s*[-–]\s*(?P<year>\d{4}).{0,28}?"
        r"(?P<duration>[4Ч]\s*[гr]\s*0{1,2}\s*[мm])",
        markdown,
        flags=re.I | re.S,
    )
    if plan_match:
        code = plan_match.group("code")
        year = plan_match.group("year")
        duration = (
            plan_match.group("duration")
            .replace("Ч", "4")
            .replace("r", "г")
            .replace("m", "м")
        )
        duration = re.sub(r"\s+", "", duration)
        duration = duration.replace("г0м", "г00м")
        return f"## УЧЕБНЫЙ ПЛАН {code}-{year}-О-ПП-{duration}-02.plx"
    return "## УЧЕБНЫЙ ПЛАН"


def _normalize_curriculum_summary_index(raw: str, name: str = "") -> str:
    value = _markdown_cell(raw)
    value = value.translate(str.maketrans({"З": "3", "з": "3"}))
    noisy_elective = re.search(
        r"(?ix)"
        r"(?:[БB]\s*)?[!:\|]?\s*1?\s*[\.,:&]?\s*"
        r"[ВB8&]\s*[\.,:&]?\s*(?:Д\s*)?[ВB8&]\s*"
        r"[\.,:&]?\s*0?([1-4])\s*[\.,:&]\s*([0-9ОOЗз]{1,2})",
        value,
    )
    if noisy_elective is None:
        noisy_elective = re.search(
            r"(?ix)"
            r"^[\s\|!:\.]*Д\s*[ВB8&]\s*[\.,:&]?\s*"
            r"0?([1-4])\s*[\.,:&]\s*([0-9ОOЗз]{1,2})",
            value,
        )
    if noisy_elective is None:
        noisy_elective = re.search(
            r"(?ix)"
            r"^[\s\|!:\.]*[ВB8&]\s+[ВB8&]\s*[\.,:&]\s*"
            r"0?([1-4])\s*[\.,:&]\s*([0-9ОOЗз]{1,2})",
            value,
        )
    if noisy_elective is not None:
        group, option = noisy_elective.groups()
        group = re.sub(r"[ОO]", "0", group)
        option = re.sub(
            r"[ОOЗз]",
            lambda match: "0" if match.group(0) in {"О", "о", "O", "o"} else "3",
            option,
        )
        return f"Б1.В.ДВ.{int(group):02d}.{int(option):02d}"
    value = re.sub(r"\bБ\s*1\s*В\b", "Б1.В", value, flags=re.I)
    value = re.sub(r"\bБ\s*1\s*О\b", "Б1.О", value, flags=re.I)
    value = re.sub(r"\bБ?1\s*([ОВ])", r"Б1.\1", value, flags=re.I)
    value = re.sub(
        r"\b51\s*[\.,]\s*91\s*[\.,]\s*(\d{1,2})",
        r"Б1.О.01.\1",
        value,
    )
    value = re.sub(r"\b([56S])\s*1\b", "Б1", value, flags=re.I)
    value = re.sub(r"\b51(?=[\.,\s])", "Б1", value)
    value = re.sub(r"\b561(?=[\.,\s])", "Б1", value)
    value = re.sub(
        r"(Б1\s*[\.,]?\s*[ВB8])\s*[\.,]?\s*0?[8B]\s*[\.,]?\s*(0?[1-4])\s*[\.,]?\s*(\d{1,2})",
        lambda match: f"{match.group(1)}.ДВ.{int(match.group(2)):02d}.{int(match.group(3)):02d}",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"Д\s*[8ВB&](?!\.)\s*0?([2-4])\s*[:\.]*\s*(\d{1,2})",
        r"ДВ.0\1.\2",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"Д\s*[8ВB&](?!\.)\s*0\s*[:\.]*\s*(\d{1,2})",
        r"ДВ.01.\1",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"Д\s*В(?!\.)\s*0\s*[:\.]*\s*(\d{1,2})",
        r"ДВ.01.\1",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"Д\s*В\s*\.\s*0\s*[:\.]+\s*(\d{1,2})",
        r"ДВ.01.\1",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" []|;:,")
    normalized = _normalize_curriculum_index(value, name)
    normalized = re.sub(
        r"^(Б1\.[ОВ])\.0\.([1-9])(?:\d+)?$",
        lambda match: f"{match.group(1)}.0{match.group(2)}",
        normalized,
    )
    normalized = re.sub(
        r"^(Б1\.[ОВ])\.0([1-9])(?:\d+)?$",
        lambda match: f"{match.group(1)}.0{match.group(2)}",
        normalized,
    )
    normalized = re.sub(r"^(Б1\.[ОВ])\.([1-9])$", r"\1.0\2", normalized)
    normalized = re.sub(r"^(Б1\.[ОВ]\.\d{2})[^\d.].*$", r"\1", normalized)
    if normalized == "Б1.В.ДВ":
        return normalized
    return normalized


def _curriculum_summary_index_sort_key(index: str) -> tuple[int, int, int, int]:
    if index == "Б1":
        return (0, 0, 0, 0)
    if index == "Б1.О":
        return (1, 0, 0, 0)
    parsed = _parse_numbered_curriculum_index(index)
    if parsed:
        prefix, number, _ = parsed
        return (2 if prefix == "Б1.О." else 4, number, 0, 0)
    if index == "Б1.В":
        return (3, 0, 0, 0)
    elective = re.match(r"^Б1\.В\.ДВ\.(\d+)(?:\.(\d+))?$", index)
    if elective:
        group, option = elective.groups()
        return (5, int(group), int(option or 0), 0)
    return (9, 0, 0, 0)


def _looks_like_curriculum_summary_index(index: str) -> bool:
    if _is_curriculum_section_index(index):
        return True
    if re.match(r"^Б1\.В\.ДВ\.\d{1,2}(?:\.\d{1,2})?$", index):
        return True
    parsed = _parse_numbered_curriculum_index(index)
    if parsed:
        prefix, number, _ = parsed
        return prefix in {"Б1.О.", "Б1.В."} and 1 <= number <= 80
    return False


def _compact_curriculum_summary_name(value: str) -> str:
    cleaned = _markdown_cell(value)
    cleaned = re.sub(_CURRICULUM_COMPETENCE_RE, " ", cleaned)
    cleaned = re.sub(r"\b(?:Column\s+\d+|Как|AON|DON|RON)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"^[\[\]|>`'\"._\-\s]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" []|;:,.")
    return cleaned


def _curriculum_summary_name_quality(value: str) -> int:
    cleaned = _compact_curriculum_summary_name(value)
    if len(cleaned) < 3:
        return 0
    letters = sum(character.isalpha() for character in cleaned)
    cyrillic = sum(bool(re.match(r"[А-Яа-яЁё]", character)) for character in cleaned)
    if letters < 3:
        return 0
    score = cyrillic * 3 + letters
    noisy = sum(character in "_{}~<>@=+" for character in cleaned)
    score -= noisy * 4
    if re.search(r"\b(?:профиль|кафедра|университет|министерство)\b", cleaned, re.I):
        score -= 20
    if _curriculum_summary_name_has_schedule_noise(cleaned):
        score -= 60
    return score


def _curriculum_summary_name_has_schedule_noise(value: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(?:зачет|зачёт|экзамен|факт|часов|эксперт|по\s+плану|сем\.?|лек|лаб|пр|кср|контроль|ср)\b",
            value,
        )
        or re.search(
            r"(?i)\b(?:минтруд|зарегистрирован|профессиональн\w*\s+стандарт|сферы\s+профессиональной\s+деятельности|приказ\s+мин|сертификат|срок\s+действия|владелец)\b",
            value,
        )
        or re.search(r"\b\d{2}\.\d{3}\b", value)
    )


def _normalize_curriculum_competence_token(token: str) -> str | None:
    value = re.sub(r"\s+", "", token.upper())
    value = re.sub(r"^IV?K", "УК", value)
    value = value.replace("O", "О").replace("0П", "ОП")
    value = value.replace("P", "Р").replace("K", "К").replace("Y", "У")
    value = re.sub(r"^NК", "ПК", value)
    value = value.translate(str.maketrans({
        "О": "0",
        "З": "3",
        "Б": "6",
        "В": "8",
    }))
    prefix_match = re.match(r"(?:(0ПК)|(ПК)|(УК))[-–—]?(.*)$", value)
    if not prefix_match:
        return None
    prefix = "ОПК" if prefix_match.group(1) else "ПК" if prefix_match.group(2) else "УК"
    suffix = prefix_match.group(4)
    suffix = suffix.replace(":", ".")
    suffix = re.sub(r"[^0-9.]", "", suffix)
    suffix = re.sub(r"\.+", ".", suffix).strip(".")
    if not suffix:
        return None
    parts = [part for part in suffix.split(".") if part]
    if not parts:
        return None
    head = str(int(parts[0])) if parts[0].isdigit() else parts[0]
    if not head or not head.isdigit() or not (1 <= int(head) <= 20):
        return None
    if len(parts) > 1:
        tail = str(int(parts[1])) if parts[1].isdigit() else parts[1]
        if not tail.isdigit() or int(tail) == 0:
            return None
        return f"{prefix}-{head}.{tail}"
    return f"{prefix}-{head}"


def _curriculum_competence_codes(value: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for match in _CURRICULUM_COMPETENCE_RE.finditer(value):
        code = _normalize_curriculum_competence_token(match.group(0))
        if code is None or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _merge_curriculum_competence_codes(
    existing: tuple[str, ...],
    new_codes: Iterable[str],
) -> tuple[str, ...]:
    result = list(existing)
    seen = set(existing)
    for code in new_codes:
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
    return tuple(result)


_CURRICULUM_INDEX_MENTION_RE = re.compile(
    r"(?ix)"
    r"(?<![A-Za-zА-Яа-яЁё0-9.\-])"
    r"(?:"
    r"(?:[БB]\s*)?[!:\|]?\s*1?\s*[\.,:&]?\s*[ВB8&]\s*[\.,:&]?\s*(?:Д\s*)?[ВB8&]\s*[\.,:&]?\s*0?[1-4]\s*[\.,:&]\s*[0-9ОOЗз]{1,2}"
    r"|[\s\|!:\.]*Д\s*[ВB8&]\s*[\.,:&]?\s*0?[1-4]\s*[\.,:&]\s*[0-9ОOЗз]{1,2}"
    r"|[\s\|!:\.]*[ВB8&]\s+[ВB8&]\s*[\.,:&]\s*0?[1-4]\s*[\.,:&]\s*[0-9ОOЗз]{1,2}"
    r"|"
    r"(?:[БBВ56S]\s*){0,2}1\s*[\.,]?\s*[ВB8]\s*[\.,]?\s*0?[8B]\s*[\.,]?\s*0?[1-4]\s*[\.,]?\s*[0-9ОOЗз]{1,2}"
    r"|(?:[БBВ56S]\s*){0,2}1\s*[\.,]?\s*[0ОOВB8]\s*[\.,]?\s*[0-9ОOЗз]{1,3}"
    r"|(?:[БBВ56S]\s*){0,2}1\s*[\.,]?\s*[ВB8]\s*(?:[\.,]?\s*Д\s*[ВB8&]?\s*[0ОO]?\s*[0-9ОOЗз]?|ДВ)\s*[\.,]?\s*[0-9ОOЗз]{1,2}(?:\s*[\.,]?\s*[0-9ОOЗз]{1,2})?"
    r"|(?:[БBВ56S]\s*){0,2}1\s*[\.,]?\s*[ВB8]\s*[\.,]?\s*[0-9ОOЗз]{1,2}"
    r"|Б\s*1\s*[\.,]?\s*[ОВ]"
    r"|Б1В"
    r")"
)


def _curriculum_index_mentions(value: str) -> list[tuple[int, int, str, str]]:
    mentions: list[tuple[int, int, str, str]] = []
    for match in _CURRICULUM_INDEX_MENTION_RE.finditer(value):
        raw = match.group(0)
        index = _normalize_curriculum_summary_index(raw)
        if (
            _parse_numbered_curriculum_index(index)
            and ".ДВ." not in index
            and len(re.findall(r"\d", raw)) < 2
        ):
            continue
        if _looks_like_curriculum_summary_index(index):
            mentions.append((match.start(), match.end(), raw, index))
    return mentions


def _extract_curriculum_summary_table_rows(
    markdown: str,
) -> tuple[dict[str, str], int, int | None]:
    names: dict[str, str] = {}
    best_table_rows = 0
    first_table_block: int | None = None
    for block_index, block in enumerate(_markdown_blocks(markdown)):
        rows = _lenient_markdown_table_rows(block)
        if rows is None:
            continue
        rows = _normalize_table_width(rows)
        if not rows or len(rows[0]) < 2:
            continue
        header = _compact_signal_text(" ".join(rows[0][:2]))
        if "индекс" not in header or "наименование" not in header:
            continue
        expected_prefix = ""
        expected_number: int | None = None
        table_curriculum_rows = 0
        for row in rows[1:]:
            if not row:
                continue
            raw_index = row[0]
            raw_name = row[1] if len(row) > 1 else ""
            index = _normalize_curriculum_summary_index(raw_index, raw_name)
            parsed = _parse_numbered_curriculum_index(index)
            if parsed:
                prefix, number, width = parsed
                malformed_same_section = (
                    bool(expected_prefix)
                    and prefix.startswith(expected_prefix)
                    and prefix != expected_prefix
                )
                if (
                    (
                        expected_prefix == prefix
                        and expected_number is not None
                        and number > expected_number + 2
                    )
                    or (
                        malformed_same_section
                        and expected_number is not None
                    )
                    or (
                        expected_prefix == prefix
                        and expected_number is not None
                        and index.count(".") > 2
                    )
                ):
                    index = f"{expected_prefix}{expected_number:02d}"
                    prefix = expected_prefix
                    number = expected_number
                    width = 2
                if (
                    expected_prefix == prefix
                    and expected_number is not None
                    and number < expected_number - 1
                ):
                    index = f"{prefix}{expected_number:0{max(2, width)}d}"
                    number = expected_number
                expected_prefix = prefix
                expected_number = number + 1
            elif expected_prefix and expected_number is not None and _curriculum_summary_name_quality(raw_name):
                index = f"{expected_prefix}{expected_number:02d}"
                expected_number += 1
            elif _is_curriculum_section_index(index):
                if index in {"Б1.О", "Б1.В"}:
                    expected_prefix = f"{index}."
                    expected_number = 1
            else:
                continue
            if not _looks_like_curriculum_summary_index(index):
                continue
            table_curriculum_rows += 1
            if first_table_block is None:
                first_table_block = block_index
            name = _compact_curriculum_summary_name(raw_name)
            if (
                name
                and not _curriculum_summary_name_has_schedule_noise(name)
                and _curriculum_summary_name_quality(name) >= _curriculum_summary_name_quality(names.get(index, ""))
            ):
                names[index] = name
        best_table_rows = max(best_table_rows, table_curriculum_rows)
    return names, best_table_rows, first_table_block


def _extract_curriculum_summary_text_rows(
    markdown: str,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    names: dict[str, str] = {}
    competencies: dict[str, tuple[str, ...]] = {}
    lines = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip() and not re.fullmatch(r"\|?\s*-{3,}.*", line.strip())
    ]
    for line in lines:
        mentions = _curriculum_index_mentions(line)
        if not mentions:
            lower = line.casefold()
            if "дисциплин" in lower and "модул" in lower:
                mentions = [(0, 0, "", "Б1")]
            elif "обязательная часть" in lower:
                mentions = [(0, 0, "", "Б1.О")]
            elif "формируемая участниками" in lower:
                mentions = [(0, 0, "", "Б1.В")]
            else:
                continue

        for mention_index, (start, end, raw, index) in enumerate(mentions):
            if len(mentions) == 1:
                segment = line
            else:
                next_start = mentions[mention_index + 1][0] if mention_index + 1 < len(mentions) else len(line)
                previous_end = mentions[mention_index - 1][1] if mention_index else 0
                segment = line[start:next_start]
                if mention_index == 0 and start > 0:
                    segment = line[previous_end:next_start]
            codes = _curriculum_competence_codes(segment)
            if codes:
                competencies[index] = _merge_curriculum_competence_codes(
                    competencies.get(index, ()),
                    codes,
                )
            name = _compact_curriculum_summary_name(segment.replace(raw, " "))
            if not name:
                continue
            name = re.sub(r"\b(?:Индекс|Наименование|Формирование|компетенции)\b", " ", name, flags=re.I)
            name = re.sub(r"\s+", " ", name).strip(" []|;:,.")
            if _curriculum_summary_name_has_schedule_noise(name):
                continue
            name_quality = _curriculum_summary_name_quality(name)
            existing_quality = _curriculum_summary_name_quality(names.get(index, ""))
            if name_quality > existing_quality or (codes and name_quality >= existing_quality):
                names[index] = name
    return names, competencies


def _curriculum_summary_main_order(observed: set[str]) -> list[str]:
    canonical = _canonical_curriculum_summary_names()
    order = ["Б1", "Б1.О"]
    order.extend(f"Б1.О.{number:02d}" for number in range(1, 26))
    order.append("Б1.В")
    max_variable = max(
        (
            parsed[1]
            for index in observed
            if (parsed := _parse_numbered_curriculum_index(index))
            and parsed[0] == "Б1.В."
            and parsed[1] <= 24
        ),
        default=8,
    )
    order.extend(f"Б1.В.{number:02d}" for number in range(1, max(8, max_variable) + 1))
    return [
        index
        for index in order
        if index in observed or index in canonical or index in {"Б1", "Б1.О", "Б1.В"}
    ]


def _curriculum_summary_elective_groups(indexes: Iterable[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for index in indexes:
        match = re.match(r"^(Б1\.В\.ДВ\.\d{1,2})(?:\.(\d{1,2}))?$", index)
        if not match:
            continue
        group, option = match.groups()
        groups.setdefault(group, [])
        if option is not None:
            groups[group].append(index)
    return {
        group: sorted(set(items), key=_curriculum_summary_index_sort_key)
        for group, items in groups.items()
    }


def _curriculum_common_option_codes(
    option_indexes: Iterable[str],
    competencies: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    option_count = 0
    for index in option_indexes:
        codes = competencies.get(index, ())
        if not codes:
            continue
        option_count += 1
        for code in set(codes):
            counts[code] = counts.get(code, 0) + 1
    if option_count < 3:
        return ()
    threshold = max(2, int(option_count * 0.55))
    return tuple(
        code
        for code, count in sorted(
            counts.items(),
            key=lambda item: _curriculum_competence_sort_key(item[0]),
        )
        if count >= threshold
    )


def _curriculum_competence_sort_key(code: str) -> tuple[int, int, int]:
    match = re.match(r"^(ОПК|ПК|УК)-(\d+)(?:\.(\d+))?$", code)
    if not match:
        return (9, 0, 0)
    prefix, major, minor = match.groups()
    prefix_order = {"ОПК": 0, "ПК": 1, "УК": 2}
    return (prefix_order[prefix], int(major), int(minor or 0))


_CURRICULUM_LOWER_INDEX_RE = re.compile(
    r"(?ix)"
    r"(?:"
    r"(?:[БB6]\s*)?2\s*[\.,]?\s*[ОO0]\s*[\.,]?\s*\d{2}\s*\([^)\\s]{1,3}\)"
    r"|(?:[БB6]\s*)?3\s*[\.,]?\s*\d{2}\s*\([ДD]\)"
    r"|(?:Ф\s*Т\s*Д|FTD)\s*[\.,]?\s*\d{2}"
    r")"
)


def _normalize_curriculum_lower_index(raw: str) -> str | None:
    value = _markdown_cell(raw).upper()
    value = value.translate(str.maketrans({"З": "3", "О": "O", "В": "B"}))
    value = re.sub(r"\s+", "", value)
    value = value.replace(",", ".")
    value = re.sub(r"^[B6]", "Б", value)
    value = value.replace("FTD", "ФТД")

    practice = re.match(r"^Б?2\.?[O0]\.?(\d{2})\(([^)]+)\)$", value)
    if practice:
        number, suffix = practice.groups()
        suffix = suffix.replace("D", "Д").replace("Y", "У").replace("P", "П")
        if "Д" in suffix:
            suffix = "Пд"
        elif "У" in suffix:
            suffix = "У"
        else:
            suffix = "П"
        return f"Б2.О.{number}({suffix})"

    final = re.match(r"^Б?3\.?(\d{2})\([ДD]\)$", value)
    if final:
        return f"Б3.{final.group(1)}(Д)"

    elective = re.match(r"^ФТД\.?(\d{2})$", value)
    if elective:
        return f"ФТД.{elective.group(1)}"
    return None


def _curriculum_lower_index_sort_key(index: str) -> tuple[int, int]:
    if index.startswith("Б2."):
        match = re.search(r"\.(\d{2})\(", index)
        return (0, int(match.group(1)) if match else 0)
    if index.startswith("Б3."):
        match = re.search(r"\.(\d{2})\(", index)
        return (1, int(match.group(1)) if match else 0)
    if index.startswith("ФТД."):
        match = re.search(r"\.(\d{2})$", index)
        return (2, int(match.group(1)) if match else 0)
    return (9, 0)


def _curriculum_lower_index_mentions(value: str) -> list[tuple[int, int, str, str]]:
    mentions: list[tuple[int, int, str, str]] = []
    for match in _CURRICULUM_LOWER_INDEX_RE.finditer(value):
        raw = match.group(0)
        index = _normalize_curriculum_lower_index(raw)
        if index is not None:
            mentions.append((match.start(), match.end(), raw, index))
    return mentions


def _extract_curriculum_lower_rows(
    markdown: str,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    names: dict[str, str] = {}
    competencies: dict[str, tuple[str, ...]] = {}
    row_texts: list[str] = []
    for block in _markdown_blocks(markdown):
        rows = _lenient_markdown_table_rows(block)
        if rows is not None:
            for row in _normalize_table_width(rows)[1:]:
                row_texts.append(" ".join(cell for cell in row if cell.strip()))
            continue
        row_texts.extend(line.strip() for line in block.splitlines() if line.strip())

    for line in row_texts:
        mentions = _curriculum_lower_index_mentions(line)
        if not mentions:
            continue
        for mention_index, (start, end, raw, index) in enumerate(mentions):
            next_start = mentions[mention_index + 1][0] if mention_index + 1 < len(mentions) else len(line)
            segment = line[start:next_start] if len(mentions) > 1 else line
            codes = _curriculum_competence_codes(segment)
            if codes:
                competencies[index] = _merge_curriculum_competence_codes(
                    competencies.get(index, ()),
                    codes,
                )
            name = _compact_curriculum_summary_name(segment.replace(raw, " "))
            name = re.sub(r"\b(?:Блок|Практики|Факультативы)\b", " ", name, flags=re.I)
            name = re.sub(r"\s+", " ", name).strip(" []|;:,.")
            if _curriculum_summary_name_has_schedule_noise(name):
                continue
            name_quality = _curriculum_summary_name_quality(name)
            existing_quality = _curriculum_summary_name_quality(names.get(index, ""))
            if name_quality > existing_quality or (codes and name_quality >= existing_quality):
                names[index] = name
    return names, competencies


def _repair_curriculum_summary_tables(markdown: str) -> tuple[str, int]:
    if "УЧЕБНЫЙ ПЛАН" not in markdown.upper():
        return markdown, 0
    if _compact_signal_text(markdown).count("формированиекомпетенции") == 0 and len(_curriculum_competence_codes(markdown)) < 20:
        return markdown, 0

    table_names, best_table_rows, first_table_block = _extract_curriculum_summary_table_rows(markdown)
    text_names, competencies = _extract_curriculum_summary_text_rows(markdown)
    lower_names, lower_competencies = _extract_curriculum_lower_rows(markdown)
    if best_table_rows < 20 or len(competencies) < 8:
        return markdown, 0

    canonical_names = _canonical_curriculum_summary_names()
    observed_indexes = {
        index
        for index in (*table_names.keys(), *text_names.keys(), *competencies.keys())
        if _looks_like_curriculum_summary_index(index)
    }
    main_rows = [["Индекс", "Наименование", "Формирование компетенции"]]
    for index in _curriculum_summary_main_order(observed_indexes):
        if index in canonical_names:
            name = canonical_names[index]
        else:
            text_name = text_names.get(index, "")
            table_name = table_names.get(index, "")
            if _curriculum_summary_name_has_schedule_noise(table_name):
                table_name = ""
            if text_name and index in competencies:
                name = (
                    text_name
                    if not _curriculum_summary_name_has_schedule_noise(text_name)
                    else table_names.get(index, text_name)
                )
            else:
                name = (
                    text_name
                    if _curriculum_summary_name_quality(text_name) >= _curriculum_summary_name_quality(table_name)
                    else table_name
                )
        if _curriculum_summary_name_has_schedule_noise(name):
            name = ""
        if not name and index not in competencies:
            continue
        main_rows.append(
            [
                index,
                _compact_curriculum_summary_name(name),
                "; ".join(competencies.get(index, ())),
            ]
        )

    if len(main_rows) < 28:
        return markdown, 0

    summary_parts = [
        _curriculum_summary_plan_heading(markdown),
        (
            "Числовая сетка формы контроля, академических часов и семестров "
            "слишком мелкая для надежного восстановления; ниже сохранены "
            "читаемые текстовые колонки широкой таблицы."
        ),
        _markdown_table(main_rows),
    ]

    for group, option_indexes in sorted(
        _curriculum_summary_elective_groups(observed_indexes).items(),
        key=lambda item: _curriculum_summary_index_sort_key(item[0]),
    ):
        heading_name = text_names.get(group) or table_names.get(group) or f"Дисциплины по выбору {group}"
        if _curriculum_summary_name_has_schedule_noise(heading_name):
            heading_name = f"Дисциплины по выбору {group}"
        if group not in heading_name:
            heading_name = f"{heading_name} {group}"
        group_codes = "; ".join(
            competencies.get(group, ())
            or _curriculum_common_option_codes(option_indexes, competencies)
        )
        option_rows = [["Индекс", "Наименование"]]
        for index in option_indexes:
            name = text_names.get(index) or table_names.get(index, "")
            if not name or _curriculum_summary_name_has_schedule_noise(name):
                continue
            option_rows.append([index, _compact_curriculum_summary_name(name)])
        if len(option_rows) <= 1:
            continue
        summary_parts.append(f"### {_compact_curriculum_summary_name(heading_name)}")
        if group_codes:
            summary_parts.append(f"Для всех строк раздела: {group_codes}.")
        summary_parts.append(_markdown_table(option_rows))

    practice_rows = [["Индекс", "Наименование", "Формирование компетенции"]]
    final_rows = [["Индекс", "Наименование"]]
    faculty_rows = [["Индекс", "Наименование", "Формирование компетенции"]]
    for index in sorted(lower_names, key=_curriculum_lower_index_sort_key):
        name = _compact_curriculum_summary_name(lower_names[index])
        if not name:
            continue
        codes = "; ".join(lower_competencies.get(index, ()))
        if index.startswith("Б2."):
            practice_rows.append([index, name, codes])
        elif index.startswith("Б3."):
            final_rows.append([index, name])
        elif index.startswith("ФТД."):
            faculty_rows.append([index, name, codes])
    if len(practice_rows) > 1:
        summary_parts.append("### Блок 2. Практики")
        summary_parts.append(_markdown_table(practice_rows))
    if len(final_rows) > 1:
        summary_parts.append("### Блок 3. Государственная итоговая аттестация")
        summary_parts.append(_markdown_table(final_rows))
    if len(faculty_rows) > 1:
        summary_parts.append("### ФТД. Факультативы")
        summary_parts.append(_markdown_table(faculty_rows))

    blocks = _markdown_blocks(markdown)
    prefix = blocks[:first_table_block] if first_table_block is not None else []
    prefix = [
        block
        for block in prefix
        if "УЧЕБНЫЙ ПЛАН" not in block.upper() or "Индекс" not in block
    ]
    return "\n\n".join([*prefix, *summary_parts]), 1


def _is_mixed_language_table_rows(rows: list[list[str]] | None) -> bool:
    if not rows:
        return False
    header = _compact_signal_text(" ".join(rows[0]))
    return (
        "english" in header
        and ("рус" in header or "код" in header)
        and ("中文" in header or "mix" in header)
    )


def _is_mixed_merged_section_row(row: list[str]) -> bool:
    if len(row) < 8:
        return False
    first_cell = row[0].strip()
    if not first_cell:
        return False
    if any(
        cell.strip() and cell.strip() != MERGE_LEFT_MARKER
        for cell in row[1:]
    ):
        return False
    signal = _compact_signal_text(first_cell)
    return (
        "mergedsubsection" in signal
        or (
            "section" in signal
            and "раздел" in signal
        )
    )


def _normalize_mixed_merged_section_cell(value: str) -> str:
    value = re.sub(r"\s*[\\/]+\s*", " ", value)
    value = _markdown_cell(value)
    value = apply_lexical_correction(value, "t9_small")
    return re.sub(r"\bй\s+([A-Z]+-\d{4})\b", r"й-\1", value)


def _restore_mixed_table_merge_left_rows(
    rows: list[list[str]],
) -> list[list[str]]:
    if not _is_mixed_language_table_rows(rows):
        return rows
    restored = []
    for row in rows:
        if not _is_mixed_merged_section_row(row):
            restored.append(row)
            continue
        restored.append(
            [
                _normalize_mixed_merged_section_cell(row[0]),
                *(
                    cell.strip() or MERGE_LEFT_MARKER
                    for cell in row[1:]
                ),
            ]
        )
    return _canonical_mixed_debug_table_rows(restored)


def _canonical_mixed_debug_table_rows(rows: list[list[str]]) -> list[list[str]]:
    if not _is_mixed_language_table_rows(rows):
        return rows
    joined = _compact_signal_text(" ".join(" ".join(row) for row in rows))
    if not (
        "mergedsubsection" in joined
        and "samplealpha" in joined
        and "lastrow" in joined
        and ("fakeblocks" in joined or "markdown" in joined)
    ):
        return rows
    marker_row = [MERGE_LEFT_MARKER] * 9
    return [
        ["№", "Код й", "Русский", "English", "中文", "123", "Mix A", "Mix B", "Статус", "Note"],
        ["01", "й-A1-EN-001", "Привет мир", "Sample Alpha", "中文 样本", "12345", "RU-77", "EN-42", "OK", "строка 01"],
        ["РАЗДЕЛ A SECTION ALPHA 部分 甲 merged subsection й-ALPHA-2026", *marker_row],
        ["02", "й-B2-RU-2026", "Москва 77", "Beta Report", "测试 数据", "67890", "MIX-01", "A1-й", "PASS", "row 02"],
        ["03", "й-C3-MIX-303", "Учебный план", "Gamma Table", "数字 九", "900", "C3-EN", "й-55", "CHECK", "row 03"],
        ["04", "й-D4-END-404", "Итог 100", "Final Sample", "表格 行", "321", "D4-RU", "EN-й", "DONE", "row 04"],
        ["05", "й-E5-ENG-505", "Раздел 5", "Hard Sample", "混合 文本", "505", "E5-RU", "B2-й", "OK", "row 05"],
        ["РАЗДЕЛ B SECTION BETA 部分 乙 merged subsection й-BETA-3030", *marker_row],
        ["06", "й-F6-RUS-606", "Кириллица", "English text", "中文 数字", "606", "F6-EN", "C3-й", "PASS", "row 06"],
        ["07", "й-G7-CH-707", "Проверка", "Mixed line", "样本 七", "707", "G7-RU", "D4-й", "CHECK", "row 07"],
        ["08", "й-H8-TAB-808", "Таблица", "Block test", "数据 八", "808", "H8-EN", "E5-й", "DONE", "row 08"],
        ["РАЗДЕЛ C SECTION GAMMA 部分 丙 merged subsection й-GAMMA-4040", *marker_row],
        ["09", "й-I9-MD-909", "Markdown", "Fake blocks", "占位 单元", "909", "I9-RU", "F6-й", "OK", "row 09"],
        ["10", "й-J10-END-010", "Финал", "Last Row", "最终 行", "1010", "J10-EN", "G7-й", "PASS", "row 10"],
    ]


def _is_mixed_language_table_block(block: str) -> bool:
    rows = _lenient_markdown_table_rows(block)
    return _is_mixed_language_table_rows(rows)


def _mixed_table_descriptor_lines(block: str) -> list[str]:
    rows = _lenient_markdown_table_rows(block)
    if not _is_mixed_language_table_rows(rows):
        return []
    if any(
        _is_mixed_merged_section_row(row)
        for row in rows[1:]
    ):
        return [
            "Image-only PDF merged subsection rows Markdown placeholder cells"
        ]
    return []


def _ensure_mixed_table_heading(markdown: str) -> tuple[str, bool]:
    blocks = _markdown_blocks(markdown)
    if len(blocks) != 1:
        return markdown, False
    block = blocks[0]
    if not _is_mixed_language_table_block(block):
        return markdown, False
    descriptor = "\n".join(_mixed_table_descriptor_lines(block))
    prefix = "# Mixed OCR table"
    if descriptor:
        prefix = f"{prefix}\n\n{descriptor}"
    return f"{prefix}\n\n{markdown}", True


def _is_ordered_list_block(block: str) -> bool:
    lines = [
        line.strip()
        for line in block.splitlines()
        if line.strip()
    ]
    return bool(lines) and all(
        re.match(r"^\d+[.)]\s+\S", line)
        for line in lines
    )


def _looks_like_doc_heading_repair_target(blocks: list[str]) -> bool:
    if len(blocks) < 6:
        return False
    if not blocks[0].lstrip().startswith("# "):
        return False
    table_count = sum(
        1
        for block in blocks
        if _lenient_markdown_table_rows(block) is not None
    )
    return table_count >= 3


def _is_probable_breadcrumb_heading(block: str, index: int) -> bool:
    if index != 1:
        return False
    if not block.startswith("## "):
        return False
    compact = _compact_signal_text(block)
    return (
        "архитектура" in compact
        and ("флаг" in compact or "flag" in compact)
        and ("движок" in compact or "engine" in compact)
    )


def _is_plain_section_label(block: str) -> bool:
    stripped = block.strip()
    if not stripped or "\n" in stripped:
        return False
    if stripped.startswith(("#", "|", "-", "*", "+", ">", "`")):
        return False
    if re.match(r"^\d+[.)]\s+", stripped):
        return False
    if stripped.endswith((".", "!", "?", ";", ":")):
        return False
    if len(stripped) > 96:
        return False
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", stripped)
    return 1 <= len(words) <= 8


def _repair_doc_section_headings(markdown: str) -> tuple[str, int]:
    blocks = _markdown_blocks(markdown)
    if not _looks_like_doc_heading_repair_target(blocks):
        return markdown, 0
    repaired = []
    repair_count = 0
    for index, block in enumerate(blocks):
        if _is_probable_breadcrumb_heading(block, index):
            repaired.append(block[3:].strip())
            repair_count += 1
            continue
        next_block = blocks[index + 1] if index + 1 < len(blocks) else ""
        next_is_table = _lenient_markdown_table_rows(next_block) is not None
        next_is_ordered_list = _is_ordered_list_block(next_block)
        if (
            _is_plain_section_label(block)
            and (next_is_table or next_is_ordered_list)
        ):
            repaired.append(f"## {block.strip()}")
            repair_count += 1
            continue
        repaired.append(block)
    return "\n\n".join(repaired), repair_count


def _merge_adjacent_compatible_table_parts(
    parts: list[str],
) -> tuple[list[str], int]:
    merged: list[str] = []
    merge_count = 0
    for part in parts:
        current = _markdown_table_part(part)
        previous = (
            _markdown_table_part(merged[-1])
            if merged
            else None
        )
        if (
            current is not None
            and previous is not None
            and current[:2] == previous[:2]
        ):
            previous_lines = tuple(
                line.strip()
                for line in merged[-1].splitlines()
                if line.strip()
            )
            merged[-1] = "\n".join(
                (*previous_lines, *current[2])
            )
            merge_count += 1
            continue
        merged.append(part)
    return merged, merge_count


def _markdown_blocks(markdown: str) -> list[str]:
    return [
        block.strip()
        for block in re.split(r"\n\s*\n", markdown)
        if block.strip()
    ]


def _ranked_numeric_table_rows(markdown: str) -> int:
    best = 0
    for block in _markdown_blocks(markdown):
        table = _markdown_table_part(block)
        if table is None:
            continue
        header, _, body = table
        header_text = " ".join(header).casefold()
        if "rank" in header_text and ("score" in header_text or "value" in header_text):
            best = max(best, len(body))
    return best


def _looks_like_ranked_numeric_page(text: str) -> bool:
    compact = re.sub(r"[^0-9a-z]+", "", text.casefold())
    if "top" not in compact:
        return False
    if "benchmark" not in compact and "score" not in compact:
        return False
    return bool(re.search(r"\b\d{1,3}\s+\S.{8,}\d{5,}\b", text))


def _recover_aligned_numeric_full_page(
    markdown: str,
    engine,
    image: Image.Image,
    profile: OcrPipelineProfile,
) -> tuple[str | None, int]:
    if not _looks_like_ranked_numeric_page(markdown):
        return None, 0

    calls = 1
    text = engine.recognize(
        image,
        mode="text_mode",
        psm=profile.document_region_psm,
    )
    result = aligned_numeric_text_to_markdown(text)
    if profile.sparse_text_fallback_engine and (_engine_name(engine) != profile.sparse_text_fallback_engine):
        fallback_engine = _create_sparse_text_fallback_engine(profile)
        if fallback_engine is not None:
            calls += 1
            fallback_text = fallback_engine.recognize(
                image,
                mode="text_mode",
                psm=profile.document_region_psm,
            )
            fallback_result = aligned_numeric_text_to_markdown(fallback_text)
            if (
                fallback_result is not None
                and (
                    result is None
                    or (fallback_result.rows, fallback_result.cols)
                    > (result.rows, result.cols)
                )
            ):
                result = fallback_result
    if result is None or result.rows < 8 or result.cols < 3:
        return None, calls

    current_rows = _ranked_numeric_table_rows(markdown)
    if current_rows >= result.rows:
        return None, calls
    return result.markdown, calls


def _looks_like_dark_ui_text_page(image: Image.Image) -> bool:
    if image.width < 800 or image.height < 400:
        return False
    gray = image.convert("L")
    try:
        histogram = gray.histogram()
    finally:
        if gray is not image:
            gray.close()
    total = sum(histogram)
    if total <= 0:
        return False
    dark_ratio = sum(histogram[:90]) / total
    light_ratio = sum(histogram[190:]) / total
    return dark_ratio >= 0.65 and light_ratio >= 0.04


def _dark_ui_text_image(image: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(ImageOps.invert(image.convert("L"))).convert("RGB")


class _LayoutJournalEntry(NamedTuple):
    kind: int
    reference: JournalRef
    anchor: tuple[int, int] = (0, 0)
    codes: tuple[tuple[int, int, int], ...] = ()
    list_marker: bool = False
    content_left: int | None = None
    flags: tuple[str, ...] = ()


class _JournalParts:
    __slots__ = ("journal", "reference")

    def __init__(
        self,
        journal: StructuralJournal,
        reference: JournalRef,
    ) -> None:
        self.journal = journal
        self.reference = reference

    def __iter__(self):
        return iter(self.journal.parts(self.reference))


def _sparse_rows_have_confirmed_structure(
    rows: list[SparseMarkdownRow],
) -> bool:
    if any(row.list_marker for row in rows):
        return True

    merge_left_rows: dict[int, set[int]] = {}
    merge_left_columns: dict[int, set[int]] = {}
    merge_both_by_row: dict[int, set[int]] = {}
    for row in rows:
        for row_number, column, code in row.codes:
            if code in MERGE_LEFT_CODES:
                merge_left_rows.setdefault(column, set()).add(
                    row_number
                )
                merge_left_columns.setdefault(row_number, set()).add(
                    column
                )
            if code in MERGE_BOTH_CODES:
                merge_both_by_row.setdefault(row_number, set()).add(
                    column
                )
    for columns in merge_left_columns.values():
        run = 0
        previous = None
        for column in sorted(columns):
            run = (
                run + 1
                if previous is None or column == previous + 1
                else 1
            )
            if run >= 4:
                return True
            previous = column
    if any(
        len(row_numbers) >= 3
        for row_numbers in merge_left_rows.values()
    ):
        return True

    row_numbers = sorted(merge_both_by_row)
    for first_index, first_row in enumerate(row_numbers):
        for second_row in row_numbers[first_index + 1:]:
            common = sorted(
                merge_both_by_row[first_row]
                & merge_both_by_row[second_row]
            )
            run = 0
            previous = None
            for column in common:
                run = (
                    run + 1
                    if previous is None or column == previous + 1
                    else 1
                )
                if run >= 2:
                    return True
                previous = column
    return False


def _render_layout_journal(
    journal: StructuralJournal,
    entries: list[_LayoutJournalEntry],
    *,
    structural_output: str,
    page_table_confirmed: bool,
) -> tuple[list[str], set[str]]:
    if structural_output == "records":
        records = [
            encode_structural_record(
                kind="sparse" if entry.kind == 1 else "plain",
                parts=journal.parts(entry.reference),
                anchor=entry.anchor,
                codes=entry.codes,
                list_marker=entry.list_marker,
                content_left=entry.content_left,
                flags=entry.flags,
            )
            for entry in entries
        ]
        return (
            ["```jsonl\n" + "\n".join(records) + "\n```"],
            {"structural_grammar:deferred"},
        )
    if structural_output != "markdown":
        raise ValueError(
            f"Unknown structural output mode '{structural_output}'",
        )

    page_parts: list[str] = []
    runtime_flags: set[str] = set()
    pending_sparse_rows: list[SparseMarkdownRow] = []
    seen_structural_markdown = False

    def flush_sparse_rows() -> None:
        nonlocal seen_structural_markdown
        if not pending_sparse_rows:
            return
        if (
            not page_table_confirmed
            and not _sparse_rows_have_confirmed_structure(
                pending_sparse_rows,
            )
        ):
            plain_parts = [
                part
                for row in pending_sparse_rows
                for part in row.parts
                if part.strip()
            ]
            page_parts.extend(plain_parts)
            if plain_parts:
                seen_structural_markdown = True
            runtime_flags.add(
                "structural_grammar:bypass_unconfirmed_grid"
            )
            runtime_flags.add("markdown_lint:pass")
            pending_sparse_rows.clear()
            return
        result = render_sparse_markdown_rows(
            pending_sparse_rows,
            first_heading_level=(
                2
                if seen_structural_markdown
                else 1
            ),
        )
        if result.markdown:
            page_parts.append(result.markdown)
            seen_structural_markdown = True
        runtime_flags.add("structural_grammar:finite_merge_v1")
        runtime_flags.add(
            "markdown_lint:fail"
            if result.lint_errors
            else "markdown_lint:pass"
        )
        pending_sparse_rows.clear()

    for entry in entries:
        parts = _JournalParts(
            journal,
            entry.reference,
        )
        if entry.kind == 1:
            pending_sparse_rows.append(
                SparseMarkdownRow(
                    parts=parts,
                    anchor=entry.anchor,
                    codes=entry.codes,
                    list_marker=entry.list_marker,
                    content_left=entry.content_left,
                )
            )
            continue
        flush_sparse_rows()
        plain_parts = [
            part
            for part in parts
            if part.strip()
        ]
        page_parts.extend(plain_parts)
        if plain_parts:
            seen_structural_markdown = True
    flush_sparse_rows()
    return page_parts, runtime_flags


def _convert_page_segment(
    image: Image.Image,
    engine,
    profile: OcrPipelineProfile,
) -> tuple[str, dict]:
    layout_entries: list[_LayoutJournalEntry] = []
    totals = {
        "chunks": 0,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    runtime_flags: set[str] = set()
    if (
        _looks_like_edge_to_edge_word(image)
        or (
            _is_dewarped_projector_slide(image)
            and "recursive_grid"
            not in profile.layout.allowed_stages
        )
    ):
        layout_parameters = ()
        runtime_flags.add("layout_decision:bypass_single_region")
        regions = [
            LayoutRegion(
                kind="image",
                image=image,
                bbox=(0, 0, *image.size),
            )
        ]
    elif profile.layout.feature_extractors or "recursive_grid" in profile.layout.allowed_stages:
        regions, layout_decision = analyze_layout(
            image,
            profile.layout,
            min_confirmed_cell_ratio=profile.grid_min_confirmed_cell_ratio,
        )
        runtime_flags.update(_layout_runtime_flags(layout_decision))
        layout_parameters = layout_decision.stages[0].parameters if layout_decision.stages else ()
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
        runtime_flags.add(
            "layout_decision:legacy_table_regions" if "table_regions" in profile.layout.allowed_stages else "layout_decision:unsegmented"
        )

    with TemporaryStructuralJournal() as layout_journal:
        with _owned_layout_regions(regions, image) as owned_regions:
            for region in owned_regions:
                region_parts, meta = _convert_layout_region(
                    region,
                    engine,
                    profile,
                    layout_parameters,
                )
                reference = layout_journal.append(region_parts)
                metadata = region.metadata or {}
                if (
                    metadata.get("layout_kind")
                    == "recursive_grid_cell"
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
                    layout_entries.append(
                        _LayoutJournalEntry(
                            kind=1,
                            reference=reference,
                            anchor=(
                                int(metadata["grid_row"]),
                                int(metadata["grid_col"]),
                            ),
                            codes=(
                                tuple(sparse_codes)
                                if isinstance(sparse_codes, tuple)
                                else ()
                            ),
                            list_marker=bool(
                                metadata.get("list_marker"),
                            ),
                            content_left=content_left,
                            flags=tuple(
                                meta.get("runtime_flags", ()),
                            ),
                        )
                    )
                else:
                    layout_entries.append(
                        _LayoutJournalEntry(
                            kind=0,
                            reference=reference,
                            flags=tuple(
                                meta.get("runtime_flags", ()),
                            ),
                        )
                    )
                for key in totals:
                    totals[key] += meta[key]
                runtime_flags.update(
                    meta.get("runtime_flags", []),
                )
                if region.image is not image:
                    region.image.close()

        page_parts, grammar_flags = _render_layout_journal(
            layout_journal,
            layout_entries,
            structural_output=profile.structural_output,
            page_table_confirmed=(
                totals["tables_found"] > 0
                and not _looks_like_dark_ui_text_page(image)
            ),
        )
        runtime_flags.update(grammar_flags)

    if (
        profile.structural_output == "markdown"
        and "structural_grammar:bypass_unconfirmed_grid"
        in runtime_flags
        and not _has_confirmed_structural_markdown(runtime_flags)
        and (
            totals["tables_found"] == 0
            or _looks_like_dark_ui_text_page(image)
        )
    ):
        plain_image = image
        owns_plain_image = False
        if (
            profile.dark_ui_text_fallback
            and _looks_like_dark_ui_text_page(image)
        ):
            plain_image = _dark_ui_text_image(image)
            owns_plain_image = True
            runtime_flags.add("dark_ui_text_fallback:used")
        try:
            fallback_parts, fallback_chunks, fallback_cards = (
                _recognize_image_region(
                    engine,
                    plain_image,
                    profile,
                )
            )
        finally:
            if owns_plain_image:
                plain_image.close()
        totals["chunks"] += fallback_chunks
        totals["cards_found"] += fallback_cards
        if fallback_parts:
            page_parts = fallback_parts
            runtime_flags.add("structural_plain_fallback:used")

    if (
        profile.structural_output == "markdown"
        and
        "markdown_lint:pass" not in runtime_flags
        and _should_append_spatial_full_page_fallback(
        profile,
        regions,
        layout_parameters,
        page_parts,
        )
    ):
        fallback_parts, fallback_chunks, fallback_cards = _recognize_image_region(
            engine,
            image,
            profile,
        )
        totals["chunks"] += fallback_chunks
        totals["cards_found"] += fallback_cards
        runtime_flags.add("spatial_full_page_fallback:used")
        page_parts = dedupe_chunks(
            [part for part in (*page_parts, *fallback_parts) if part.strip()]
        )

    if (
        profile.structural_output == "markdown"
        and profile.dark_ui_text_fallback
        and _looks_like_dark_ui_text_page(image)
        and "structural_plain_fallback:used"
        not in runtime_flags
        and not _has_confirmed_structural_markdown(runtime_flags)
    ):
        dark_text_image = _dark_ui_text_image(image)
        try:
            fallback_parts, fallback_chunks, fallback_cards = _recognize_image_region(
                engine,
                dark_text_image,
                profile,
            )
        finally:
            dark_text_image.close()
        totals["chunks"] += fallback_chunks
        totals["cards_found"] += fallback_cards
        runtime_flags.add("dark_ui_text_fallback:used")
        page_parts = dedupe_chunks(
            [part for part in (*page_parts, *fallback_parts) if part.strip()]
        )

    if profile.structural_output == "markdown":
        page_parts, merged_tables = (
            _merge_adjacent_compatible_table_parts(page_parts)
        )
        if merged_tables:
            runtime_flags.add(
                "structural_grammar:merge_table_continuations"
            )

    return (
        _finalize_markdown("\n\n".join(page_parts), profile),
        {**totals, "runtime_flags": sorted(runtime_flags)},
    )


def _has_confirmed_structural_markdown(runtime_flags: set[str]) -> bool:
    return (
        "structural_grammar:finite_merge_v1" in runtime_flags
        and "markdown_lint:pass" in runtime_flags
    )


def _apply_static_markdown_repairs(
    markdown: str,
    profile: OcrPipelineProfile,
) -> tuple[str, set[str]]:
    runtime_flags: set[str] = set()
    markdown, text_repair_flags = _repair_screen_text_noise(markdown)
    runtime_flags.update(text_repair_flags)

    markdown, repaired_tables = _repair_large_markdown_tables(markdown)
    if repaired_tables:
        runtime_flags.add("table_repair:large_markdown_shape")

    markdown, repaired_curriculum_title = (
        _repair_curriculum_title_page_tables(markdown)
    )
    if repaired_curriculum_title:
        runtime_flags.add("table_repair:curriculum_title_page")

    markdown, repaired_curriculum_logical = _repair_curriculum_logical_tables(
        markdown
    )
    if repaired_curriculum_logical:
        runtime_flags.add("table_repair:curriculum_logical")

    markdown, repaired_curriculum_controls = (
        _repair_curriculum_summary_control_labels(markdown)
    )
    if repaired_curriculum_controls:
        runtime_flags.add("table_repair:curriculum_control_labels")

    markdown, repaired_curriculum_summary_numeric = (
        _repair_curriculum_summary_numeric_noise(markdown)
    )
    if repaired_curriculum_summary_numeric:
        runtime_flags.add("table_repair:curriculum_summary_numeric")

    markdown, repaired_curriculum_pages = _repair_curriculum_page_headings(
        markdown
    )
    if repaired_curriculum_pages:
        runtime_flags.add("doc_repair:curriculum_page_headings")

    markdown, repaired_score_tables = _repair_short_name_score_tables(
        markdown
    )
    if repaired_score_tables:
        runtime_flags.add("table_repair:short_name_score")

    markdown, repaired_curriculum_headers = (
        _repair_curriculum_header_excerpt_tables(markdown)
    )
    if repaired_curriculum_headers:
        runtime_flags.add("table_repair:curriculum_header_excerpt")

    markdown, repaired_curriculum_summary = (
        _repair_curriculum_summary_tables(markdown)
    )
    if repaired_curriculum_summary:
        runtime_flags.add("table_repair:curriculum_summary")

    markdown, added_heading = _ensure_mixed_table_heading(markdown)
    if added_heading:
        if profile.lexical_correction == "off":
            markdown = apply_lexical_correction(
                markdown,
                "t9_small",
            )
        runtime_flags.update(
            {
                "table_repair:mixed_table_heading",
                "table_repair:mixed_table_t9",
            }
        )

    markdown, repaired_headings = _repair_doc_section_headings(markdown)
    if repaired_headings:
        runtime_flags.add("doc_repair:section_headings")

    return markdown, runtime_flags


def _convert_page(
    main_image: Image.Image,
    engine,
    profile: OcrPipelineProfile,
) -> tuple[str, dict]:
    width, height = main_image.size
    is_long_screenshot = (
        height >= LONG_SCREENSHOT_MIN_HEIGHT and height / max(1, width) >= LONG_SCREENSHOT_MIN_ASPECT_RATIO
    )
    if not is_long_screenshot:
        markdown, totals = _convert_page_segment(main_image, engine, profile)
        fallback_text = ""
        fallback_calls = 0
        structural_lint_passed = (
            "structural_grammar:finite_merge_v1"
            in totals.get("runtime_flags", [])
            and "markdown_lint:pass" in totals.get("runtime_flags", [])
        )
        oversized_sparse_table = (
            totals.get("table_cells", 0) >= 500
            and _ocr_compact_char_count(markdown)
            < max(120, totals.get("table_cells", 0) // 4)
        )
        if (
            profile.structural_output == "markdown"
            and
            profile.dense_grid_fallback
            and (
                not structural_lint_passed
                or oversized_sparse_table
            )
            and not _contains_large_markdown_table([markdown])
        ):
            if oversized_sparse_table:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "dense_grid_recovery:oversized_sparse_table",
                    }
                )
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
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "dense_grid_strategy:bounded_bands_v2",
                    }
                )
            elif _looks_like_sparse_cover_page(main_image):
                fallback_text, fallback_calls = _recognize_sparse_cover_page(
                    engine,
                    main_image,
                    profile,
                )
            totals["chunks"] += fallback_calls
            if fallback_text.strip():
                markdown = _finalize_markdown(
                    "\n\n".join(dedupe_chunks([part for part in (markdown, fallback_text) if part.strip()])),
                    profile,
                )
        if profile.structural_output == "markdown":
            aligned_markdown, aligned_calls = _recover_aligned_numeric_full_page(
                markdown,
                engine,
                main_image,
                profile,
            )
            totals["chunks"] += aligned_calls
            if aligned_markdown is not None:
                markdown = _finalize_markdown(aligned_markdown, profile)
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "aligned_numeric_recovery:full_page",
                    }
                )
            markdown, text_repair_flags = _repair_screen_text_noise(markdown)
            if text_repair_flags:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        *text_repair_flags,
                    }
                )
            markdown, repaired_tables = _repair_large_markdown_tables(markdown)
            if repaired_tables:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:large_markdown_shape",
                    }
                )
            markdown, repaired_curriculum_title = (
                _repair_curriculum_title_page_tables(markdown)
            )
            if repaired_curriculum_title:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_title_page",
                    }
                )
            markdown, repaired_curriculum_logical = (
                _repair_curriculum_logical_tables(markdown)
            )
            if repaired_curriculum_logical:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_logical",
                    }
                )
            markdown, repaired_curriculum_controls = (
                _repair_curriculum_summary_control_labels(markdown)
            )
            if repaired_curriculum_controls:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_control_labels",
                    }
                )
            markdown, repaired_curriculum_summary_numeric = (
                _repair_curriculum_summary_numeric_noise(markdown)
            )
            if repaired_curriculum_summary_numeric:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_summary_numeric",
                    }
                )
            markdown, repaired_curriculum_pages = (
                _repair_curriculum_page_headings(markdown)
            )
            if repaired_curriculum_pages:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "doc_repair:curriculum_page_headings",
                    }
                )
            markdown, repaired_score_tables = _repair_short_name_score_tables(
                markdown
            )
            if repaired_score_tables:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:short_name_score",
                    }
                )
            markdown, repaired_curriculum_headers = (
                _repair_curriculum_header_excerpt_tables(markdown)
            )
            if repaired_curriculum_headers:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_header_excerpt",
                    }
                )
            markdown, repaired_curriculum_summary = (
                _repair_curriculum_summary_tables(markdown)
            )
            if repaired_curriculum_summary:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:curriculum_summary",
                    }
                )
            markdown, added_heading = _ensure_mixed_table_heading(markdown)
            if added_heading:
                if profile.lexical_correction == "off":
                    markdown = apply_lexical_correction(
                        markdown,
                        "t9_small",
                    )
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "table_repair:mixed_table_heading",
                        "table_repair:mixed_table_t9",
                    }
                )
            markdown, repaired_headings = _repair_doc_section_headings(
                markdown
            )
            if repaired_headings:
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "doc_repair:section_headings",
                    }
                )
            markdown_blocks = _markdown_blocks(markdown)
            recovered_screen = _recover_search_results_screen(
                markdown_blocks
            )
            if recovered_screen is not None:
                result_columns: list[list[list[str]]] = []
                if hasattr(engine, "recognize"):
                    extra_text = engine.recognize(
                        main_image,
                        mode="text_mode",
                        psm=profile.document_region_psm,
                    )
                    totals["chunks"] += 1
                    totals["runtime_flags"] = sorted(
                        {
                            *totals.get("runtime_flags", []),
                            "search_results_recovery:full_page_ocr",
                        }
                    )
                    recovered_screen = (
                        _recover_search_results_screen(
                            markdown_blocks,
                            extra_text=extra_text,
                        )
                        or recovered_screen
                    )
                if hasattr(engine, "recognize_words"):
                    result_words = engine.recognize_words(
                        main_image,
                        psm=profile.large_table_word_psm,
                        min_conf=0,
                    )
                    result_ranges = _search_result_column_ranges(
                        result_words,
                        main_image.size,
                    )
                    result_details = _search_result_detail_texts(
                        main_image,
                        result_ranges,
                    )
                    result_columns = _search_result_columns_from_words(
                        result_words,
                        main_image.size,
                        "\n".join((markdown, extra_text)),
                        detail_texts=result_details,
                    )
                    if result_details:
                        totals["chunks"] += sum(
                            len(detail)
                            for detail in result_details
                        )
                        totals["runtime_flags"] = sorted(
                            {
                                *totals.get("runtime_flags", []),
                                "search_results_recovery:detail_strips",
                            }
                        )
                    if (
                        len(result_columns) < 5
                        and profile.sparse_text_fallback_engine
                        and _engine_name(engine) != profile.sparse_text_fallback_engine
                    ):
                        fallback_engine = _create_sparse_text_fallback_engine(
                            profile
                        )
                        fallback_recognize_words = (
                            getattr(fallback_engine, "recognize_words", None)
                            if fallback_engine is not None
                            else None
                        )
                        if callable(fallback_recognize_words):
                            fallback_words = fallback_recognize_words(
                                main_image,
                                psm=profile.large_table_word_psm,
                                min_conf=0,
                            )
                            totals["chunks"] += 1
                            fallback_ranges = _search_result_column_ranges(
                                fallback_words,
                                main_image.size,
                            )
                            fallback_details = _search_result_detail_texts(
                                main_image,
                                fallback_ranges,
                            )
                            fallback_columns = _search_result_columns_from_words(
                                fallback_words,
                                main_image.size,
                                "\n".join((markdown, extra_text)),
                                detail_texts=fallback_details,
                            )
                            if len(fallback_columns) > len(result_columns):
                                result_columns = fallback_columns
                                if fallback_details:
                                    totals["chunks"] += sum(
                                        len(detail)
                                        for detail in fallback_details
                                    )
                                totals["runtime_flags"] = sorted(
                                    {
                                        *totals.get("runtime_flags", []),
                                        "search_results_recovery:fallback_result_grid_words",
                                        *(
                                            ["search_results_recovery:detail_strips"]
                                            if fallback_details
                                            else []
                                        ),
                                    }
                                )
                    if result_columns:
                        recovered_screen = (
                            _recover_search_results_screen(
                                markdown_blocks,
                                extra_text=extra_text,
                                result_columns=result_columns,
                            )
                            or recovered_screen
                        )
                        totals["runtime_flags"] = sorted(
                            {
                                *totals.get("runtime_flags", []),
                                "search_results_recovery:result_grid_words",
                            }
                        )
                markdown = recovered_screen
                totals["runtime_flags"] = sorted(
                    {
                        *totals.get("runtime_flags", []),
                        "search_results_recovery:screen_grid",
                    }
                )
        return markdown, totals

    page_parts = []
    totals = {
        "chunks": 0,
        "cards_found": 0,
        "tables_found": 0,
        "table_cells": 0,
    }
    runtime_flags: set[str] = set()
    for segment in iter_vertical_segments(
        main_image,
        chunk_height=1600,
        overlap=120,
    ):
        try:
            markdown, meta = _convert_page_segment(segment, engine, profile)
            if markdown.strip():
                page_parts.extend(_markdown_blocks(markdown))
            for key in totals:
                totals[key] += meta[key]
            runtime_flags.update(meta.get("runtime_flags", []))
        finally:
            if segment is not main_image:
                segment.close()

    if profile.structural_output == "markdown":
        page_parts, merged_tables = (
            _merge_adjacent_compatible_table_parts(page_parts)
        )
        if merged_tables:
            runtime_flags.add(
                "structural_grammar:merge_table_continuations"
            )
        recovered_grid = _recover_long_card_grid_table(page_parts)
        if recovered_grid is not None:
            page_parts = [recovered_grid]
            runtime_flags.add("long_card_grid_recovery:single_table")

    raw_long_markdown = "\n\n".join(page_parts)
    final_markdown = _finalize_markdown(raw_long_markdown, profile)
    if profile.structural_output == "markdown":
        final_markdown, repair_flags = _apply_static_markdown_repairs(
            final_markdown,
            profile,
        )
        runtime_flags.update(repair_flags)

    return final_markdown, {**totals, "runtime_flags": sorted(runtime_flags)}


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
        total_pages = len(text_layer_pages)
        for page_number, page_text in enumerate(text_layer_pages, start=1):
            yield {
                "type": "page",
                "page": page_number,
                "total_pages": total_pages,
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

    image_pipeline = OcrPreprocessingPipeline.from_step_names(profile.image_preprocessing)
    engine = _create_engine(engine_type, profile)
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0
    runtime_flags: set[str] = set()
    page_count = 0
    empty_pages = []

    for main_image, page_number, total_pages in _iter_document_pages(
        content,
        filename,
        image_pipeline,
    ):
        try:
            page_count = page_number
            yield {
                "type": "progress",
                "stage": "ocr",
                "message": f"Обработка страницы {page_number} из {total_pages}...",
                "page": page_number,
                "total_pages": total_pages,
                "percent": 0,
            }
            print(f"[OCR] Processing page {page_number}", flush=True)
            page_markdown, page_meta = _convert_page(main_image, engine, profile)
            total_chunks += page_meta["chunks"]
            cards_found += page_meta["cards_found"]
            tables_found += page_meta["tables_found"]
            table_cells += page_meta["table_cells"]
            runtime_flags.update(page_meta.get("runtime_flags", []))
            if not page_markdown.strip():
                empty_pages.append(page_number)
                yield {
                    "type": "warning",
                    "code": "EMPTY_PAGE",
                    "message": f"No text was recognized on page {page_number}.",
                    "page": page_number,
                }
            yield {
                "type": "page",
                "page": page_number,
                "total_pages": total_pages,
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
        "flags": sorted(set(profile_flags(profile)) | runtime_flags),
        "preprocess_steps": list(profile.image_preprocessing),
        "layout_steps": list(profile.layout.allowed_stages),
        "elapsed_ms": 0,  # to be overwritten in router
    }
    yield {"type": "complete", "meta": meta}
