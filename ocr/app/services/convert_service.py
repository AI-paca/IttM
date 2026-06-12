import tempfile
from io import BytesIO
from pathlib import Path
from typing import Iterator, Tuple

from PIL import Image

from app.chunking.dedupe import dedupe_chunks
from app.chunking.vertical import (
    analyze_document_layout,
    erase_table_lines_for_ocr,
    LayoutRegion,
    logical_table_layout,
    split_by_blank_bands,
    split_vertical,
    table_layout_to_markdown,
    table_words_to_markdown,
    wide_curriculum_table_to_markdown,
)
from app.engines.auto_engine import AutoEngine
from app.formatting.markdown_formatter import MarkdownFormatter
from app.pipeline_config import OcrPipelineProfile, resolve_pipeline_profile
from app.preprocessing import OcrPreprocessingPipeline

Image.MAX_IMAGE_PIXELS = None


def _prepared_image(image: Image.Image, image_pipeline: OcrPreprocessingPipeline) -> Image.Image:
    image.load()
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba_image = image.convert("RGBA")
        try:
            base_image = Image.new("RGB", rgba_image.size, (255, 255, 255))
            base_image.paste(rgba_image, mask=rgba_image.split()[3])
        finally:
            rgba_image.close()
    else:
        base_image = image.convert("RGB")

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

            for page_number in range(1, page_count + 1):
                print(f"[PDF] Rendering page {page_number}/{page_count}", flush=True)
                pages = convert_from_path(
                    str(pdf_path),
                    dpi=300,
                    fmt="png",
                    first_page=page_number,
                    last_page=page_number,
                    thread_count=1,
                )
                if not pages:
                    raise ValueError(f"PDF page {page_number} could not be rendered")
                page = pages[0]
                try:
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


def _recognize_image_region(engine, image: Image.Image) -> Tuple[list[str], int, int]:
    width, height = image.size
    if height <= 1600 or (width > 0 and height / width <= 1.8):
        return [engine.recognize(image, mode="text_mode")], 1, 0

    cards = split_by_blank_bands(image)
    if cards and len(cards) > 1:
        page_parts = []
        cards_found = 0
        for i, card_img in enumerate(cards):
            try:
                if card_img.size[1] < 50:
                    continue
                cards_found += 1
                card_text = engine.recognize(card_img, mode="text_mode")
                page_parts.append(_format_card_to_markdown(card_text, i))
            finally:
                card_img.close()
        return page_parts, cards_found, cards_found

    chunks = split_vertical(image, chunk_height=1200, overlap=100)
    page_texts = []
    for chunk in chunks:
        try:
            page_texts.append(engine.recognize(chunk, mode="text_mode"))
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
    words = recognize_words(erase_table_lines_for_ocr(scaled), psm=psm, min_conf=min_conf)
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


def _merge_wide_table_left_strip_words(engine, image: Image.Image, table, words: list[dict]) -> list[dict]:
    if table.cols < 45 or len(table.x_lines) < 3:
        return words

    left_limit = min(image.size[0], max(1, int(table.x_lines[2])))
    if left_limit <= 1:
        return words

    left_words = _recognize_scaled_words(
        engine, image.crop((0, 0, left_limit, image.size[1])), scale=3, psm=6, min_conf=0
    )
    if len(left_words) < 10:
        return words

    remaining = []
    for word in words:
        bbox = word.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        left, _, right, _ = bbox
        if (left + right) / 2 > left_limit:
            remaining.append(word)

    return [*remaining, *left_words]


def _recognize_table_words(engine, image: Image.Image, table, *, single_pass: bool = False) -> list[dict]:
    recognize_words = getattr(engine, "recognize_words", None)
    if not callable(recognize_words):
        return []

    width, height = image.size
    if single_pass or (table.cols <= 4 and height <= 3600):
        words = recognize_words(erase_table_lines_for_ocr(image), psm=6, min_conf=18)
        if single_pass:
            return _merge_wide_table_left_strip_words(engine, image, table, words)
        return words

    x_segments = _line_bounded_segments(table.x_lines, width, max_span=1700)
    y_segments = _line_bounded_segments(table.y_lines, height, max_span=1300)
    psm = 11 if len(table.cells) > 200 else 6
    min_conf = 18 if len(table.cells) <= 200 else 25

    words = []
    for y1, y2 in y_segments:
        for x1, x2 in x_segments:
            tile = image.crop((x1, y1, x2, y2))
            tile_words = recognize_words(erase_table_lines_for_ocr(tile), psm=psm, min_conf=min_conf)
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

    return words


async def convert(
    path: Path,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
) -> Tuple[str, dict]:
    return convert_bytes(
        path.read_bytes(),
        filename=path.name,
        engine_type=engine_type,
        pipeline_profile=pipeline_profile,
    )


def convert_bytes(
    content: bytes,
    filename: str,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
) -> Tuple[str, dict]:
    markdown_parts = []
    meta = None
    for event in iter_convert_bytes(
        content,
        filename=filename,
        engine_type=engine_type,
        pipeline_profile=pipeline_profile,
    ):
        if event["type"] == "page" and event["markdown"].strip():
            markdown_parts.append(event["markdown"])
        elif event["type"] == "complete":
            meta = event["meta"]

    if meta is None:
        raise ValueError("OCR conversion did not produce completion metadata.")
    return "\n\n---\n\n".join(markdown_parts), meta


def _create_engine(engine_type: str):
    if engine_type == "tesseract":
        from app.engines.tesseract_engine import TesseractEngine

        return TesseractEngine()
    if engine_type == "easyocr":
        from app.engines.easyocr_engine import EasyOcrEngine

        engine = EasyOcrEngine()
        if not engine.available():
            raise ValueError(f"EasyOCR is not installed or initialization failed: {engine.info().get('init_error')}")
        return engine
    return AutoEngine(prefer_tesseract=True)


def _convert_page(
    main_image: Image.Image,
    engine,
    profile: OcrPipelineProfile,
) -> tuple[str, dict]:
    page_parts = []
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0
    regions = (
        analyze_document_layout(main_image)
        if "table_layout" in profile.layout_analysis
        else [LayoutRegion(kind="image", image=main_image, bbox=(0, 0, *main_image.size))]
    )

    for region in regions:
        if region.kind == "table" and region.table is not None:
            if _should_segment_table_region(region.image, region.table):
                region_parts, region_chunks, region_cards = _recognize_image_region(
                    engine,
                    region.image,
                )
                total_chunks += region_chunks
                cards_found += region_cards
                page_parts.extend(part for part in region_parts if part.strip())
                continue

            is_wide_table = region.table.cols >= 45
            table_layout = region.table if is_wide_table else logical_table_layout(region.image, region.table)
            tables_found += 1
            table_cells += len(table_layout.cells)
            total_chunks += len(table_layout.cells)

            table_md = ""
            table_words = _recognize_table_words(
                engine,
                region.image,
                table_layout,
                single_pass=is_wide_table and region.image.size[1] <= MAX_DIRECT_TABLE_HEIGHT,
            )
            if table_words:
                table_md = (
                    wide_curriculum_table_to_markdown(table_layout, table_words)
                    if is_wide_table
                    else table_words_to_markdown(table_layout, table_words)
                )

            if not table_md.strip():
                table_md = table_layout_to_markdown(
                    region.image,
                    table_layout,
                    lambda cell_image: _recognize_table_cell(engine, cell_image),
                )
            if table_md.strip():
                page_parts.append(table_md)
            continue

        region_parts, region_chunks, region_cards = _recognize_image_region(
            engine,
            region.image,
        )
        total_chunks += region_chunks
        cards_found += region_cards
        page_parts.extend(part for part in region_parts if part.strip())

    return (
        MarkdownFormatter.format_text("\n\n".join(page_parts)),
        {
            "chunks": total_chunks,
            "cards_found": cards_found,
            "tables_found": tables_found,
            "table_cells": table_cells,
        },
    )


def iter_convert_bytes(
    content: bytes,
    filename: str,
    engine_type: str = "auto",
    pipeline_profile: OcrPipelineProfile | None = None,
) -> Iterator[dict]:
    """
    Convert a document page by page and yield page/completion events.

    Args:
        content: Uploaded document bytes.
        filename: Original filename used to distinguish PDF from images.
        engine_type: 'auto' (Tesseract first), 'tesseract' (core), or 'easyocr' (high-quality)
    """
    profile = pipeline_profile or resolve_pipeline_profile(engine_type)
    image_pipeline = OcrPreprocessingPipeline.from_step_names(profile.image_preprocessing)
    engine = _create_engine(engine_type)
    total_chunks = 0
    cards_found = 0
    tables_found = 0
    table_cells = 0
    page_count = 0

    for main_image in _iter_document_images(content, filename, image_pipeline):
        try:
            page_count += 1
            print(f"[OCR] Processing page {page_count}", flush=True)
            page_markdown, page_meta = _convert_page(main_image, engine, profile)
            total_chunks += page_meta["chunks"]
            cards_found += page_meta["cards_found"]
            tables_found += page_meta["tables_found"]
            table_cells += page_meta["table_cells"]
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
        "chunks": total_chunks,
        "cards_found": cards_found,
        "tables_found": tables_found,
        "table_cells": table_cells,
        "pages": page_count,
        "pipeline": profile.name,
        "preprocess_steps": list(profile.image_preprocessing),
        "layout_steps": list(profile.layout_analysis),
        "elapsed_ms": 0,  # to be overwritten in router
    }
    yield {"type": "complete", "meta": meta}
